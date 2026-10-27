# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from __future__ import annotations

from typing import TYPE_CHECKING

from .prompt_agent_tools import *  # noqa: F403,F401  # NOSONAR


if TYPE_CHECKING:
    from core.application.registry.step_output_contract import StepOutputContract


class PromptAgentArtifactMixin:
    """Artifact parsing and contract backfill helpers."""

    def _parse_artifacts(self, response_text: str, skill: str) -> dict:
        """Try to extract structured artifacts from LLM response.

        Looks for a phase-result JSON object first; falls back to storing the raw text
        under the primary output key for this skill.
        """
        primary_key = self._primary_output_key(skill)

        candidate = self._extract_phase_result_candidate(response_text)
        if candidate:
            data = candidate
            if self._looks_like_phase_result_candidate(data):
                data = dict(data.get("artifacts") or {})
            extracted = self._extract_labeled_artifacts(response_text, skill)
            for key, value in extracted.items():
                data.setdefault(key, value)
            salvaged = self._extract_jsonish_contract_artifacts(response_text, skill)
            for key, value in salvaged.items():
                data.setdefault(key, value)
            if primary_key and primary_key not in data:
                data[primary_key] = response_text
            self._normalize_gate_artifacts(skill, data)
            return data

        json_payloads = self._iter_json_fence_payloads(response_text)
        if json_payloads:
            try:
                data = json.loads(json_payloads[0])
                if isinstance(data, dict):
                    extracted = self._extract_labeled_artifacts(response_text, skill)
                    for key, value in extracted.items():
                        data.setdefault(key, value)
                    salvaged = self._extract_jsonish_contract_artifacts(response_text, skill)
                    for key, value in salvaged.items():
                        data.setdefault(key, value)
                    if primary_key and primary_key not in data:
                        data[primary_key] = response_text
                    self._normalize_gate_artifacts(skill, data)
                    return data
            except json.JSONDecodeError:
                pass

        stripped = (response_text or "").strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                data = json.loads(stripped)
                if isinstance(data, dict):
                    extracted = self._extract_labeled_artifacts(response_text, skill)
                    for key, value in extracted.items():
                        data.setdefault(key, value)
                    salvaged = self._extract_jsonish_contract_artifacts(response_text, skill)
                    for key, value in salvaged.items():
                        data.setdefault(key, value)
                    if primary_key and primary_key not in data:
                        data[primary_key] = response_text
                    self._normalize_gate_artifacts(skill, data)
                    return data
            except json.JSONDecodeError:
                pass

        # Map skill to primary output key
        artifacts = self._extract_labeled_artifacts(response_text, skill)
        salvaged = self._extract_jsonish_contract_artifacts(response_text, skill)
        for key, value in salvaged.items():
            artifacts.setdefault(key, value)
        key = primary_key or "output"
        artifacts.setdefault(key, response_text)
        self._normalize_gate_artifacts(skill, artifacts)
        return artifacts

    @staticmethod
    def _decode_jsonish_field(response_text: str, field: str) -> Any:
        """Decode a single JSON-ish field without requiring the whole response to parse.

        Provider responses can be mostly valid phase_result_v2 JSON while one later
        string is malformed by redaction/escaping. Decoding individual contract fields
        keeps gate routing based on the explicit verdict instead of the broken tail.
        """
        text = str(response_text or "")
        match = re.search(rf'"{re.escape(field)}"\s*:\s*', text)
        if not match:
            return None

        fragment = text[match.end() :].lstrip()
        if not fragment:
            return None

        if re.match(r"\[\s*\]", fragment):
            return []
        if re.match(r"\{\s*\}", fragment):
            return {}
        if re.match(r'""', fragment):
            return ""
        if re.match(r"null\b", fragment):
            return None

        try:
            value, _ = json.JSONDecoder().raw_decode(fragment)
            return value
        except json.JSONDecodeError:
            return None

    @classmethod
    def _extract_jsonish_contract_artifacts(cls, response_text: str, skill: str) -> dict:
        _ = cls
        _ = response_text
        _ = skill
        return {}

    @staticmethod
    def _normalize_check_rows(value: Any, *, skipped: bool = False) -> list[dict]:
        """Normalize check payloads from list/dict/string forms into row dicts."""

        def _append_row(rows: list[dict], check_name: Any, raw: Any) -> None:
            row = dict(raw) if isinstance(raw, dict) else {}
            check = str(
                row.get("check") or row.get("key") or row.get("name") or check_name or ""
            ).strip()
            if not check:
                return
            row["check"] = check

            if skipped:
                if not str(row.get("reason") or "").strip():
                    if isinstance(raw, str) and raw.strip():
                        row["reason"] = raw.strip()
                    else:
                        fallback = row.get("detail") or row.get("message") or row.get("status")
                        if fallback not in (None, ""):
                            row["reason"] = str(fallback).strip()
            else:
                if not str(row.get("result") or "").strip():
                    if isinstance(raw, bool):
                        row["result"] = "pass" if raw else "fail"
                    elif isinstance(raw, str) and raw.strip():
                        row["result"] = raw.strip()
                    else:
                        fallback = (
                            row.get("status")
                            or row.get("verdict")
                            or row.get("outcome")
                            or row.get("passed")
                        )
                        if isinstance(fallback, bool):
                            row["result"] = "pass" if fallback else "fail"
                        elif fallback not in (None, ""):
                            row["result"] = str(fallback).strip()

            rows.append(row)

        rows: list[dict] = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _append_row(rows, None, item)
                elif str(item or "").strip():
                    _append_row(rows, item, item)
        elif isinstance(value, dict):
            for check_name, raw in value.items():
                _append_row(rows, check_name, raw)
        elif str(value or "").strip():
            _append_row(rows, value, value)
        return rows

    @staticmethod
    def _normalize_issue_list(value: Any) -> list[str]:
        """Normalize issue/warning payloads into a stable list of strings."""
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, dict):
            items: list[str] = []
            for key, raw in value.items():
                key_text = str(key).strip()
                if raw in (None, "", [], {}):
                    if key_text:
                        items.append(key_text)
                    continue
                if isinstance(raw, dict):
                    detail = str(
                        raw.get("reason") or raw.get("detail") or raw.get("message") or ""
                    ).strip()
                    text = f"{key_text}: {detail}" if key_text and detail else key_text or detail
                else:
                    detail = str(raw).strip()
                    text = f"{key_text}: {detail}" if key_text and detail else key_text or detail
                if text:
                    items.append(text)
            return items
        text = str(value or "").strip()
        return [text] if text else []

    @classmethod
    def _normalize_gate_artifacts(cls, skill: str, artifacts: dict) -> None:
        """Canonicalize generic artifact collections without skill-specific branching."""
        _ = skill
        if "warnings" in artifacts:
            artifacts["warnings"] = cls._normalize_issue_list(artifacts.get("warnings"))

    @staticmethod
    def _published_block_outputs_to_contract_rows(block: dict) -> list[dict]:
        rows: list[dict] = []
        outputs = list((block or {}).get("outputs") or (block or {}).get("_outputs") or [])
        for row in outputs:
            if not isinstance(row, dict):
                continue
            if not bool(row.get("exposed_by_default", True)):
                continue
            name = str(row.get("field_key") or "").strip()
            if not name:
                continue
            from core.application.registry.output_contract_fields import (
                published_output_description,
            )

            description = published_output_description(row)
            rows.append(
                {
                    "name": name,
                    "type": str(row.get("value_type") or "text").strip() or "text",
                    "description": description,
                    "required": bool(row.get("required_by_default", False)),
                }
            )
        return rows

    @staticmethod
    def _extract_step_published_block_key(step: dict | None) -> str:
        raw = step if isinstance(step, dict) else {}
        override = raw.get("override")
        if isinstance(override, str):
            try:
                parsed = json.loads(override)
            except Exception:
                parsed = {}
            override = parsed if isinstance(parsed, dict) else {}
        if not isinstance(override, dict):
            override = {}
        for key in ("published_block_key", "block_key"):
            value = str(override.get(key) or "").strip()
            if value:
                return value
        return ""

    def _resolve_final_response_schema(
        self,
        skill: str,
        step: dict | None = None,
        state: dict | None = None,
        output_contract: StepOutputContract | None = None,
    ) -> dict:
        """Build final structured-output schema from explicit step block outputs (fallback: skill outputs)."""
        if output_contract is not None:
            return build_phase_result_json_schema(output_contract.rows)
        outputs: list[dict] = []
        if skill:
            try:
                from core.application.management.scope import scope_from_state
                from core.application.registry.registry_manager import RegistryManager

                db = RegistryManager.instance().db_store
                if db:
                    # Confine published-block resolution to the runtime account so
                    # a cross-account block contract cannot be projected into the
                    # response schema. Global ('system') blocks remain resolvable.
                    account_id = scope_from_state(state or {}).account_id
                    explicit_block_key = self._extract_step_published_block_key(step)
                    if explicit_block_key:
                        full = (
                            db.get_published_block_contract(
                                explicit_block_key, account_id=account_id
                            )
                            or {}
                        )
                        outputs = self._published_block_outputs_to_contract_rows(full)
                    if not outputs:
                        outputs = db.get_skill_outputs(skill) or []
            except Exception:
                outputs = []
        return build_phase_result_json_schema(outputs)

    @staticmethod
    def _looks_like_phase_result_candidate(data: dict) -> bool:
        """Detect explicit phase-result envelopes."""
        return bool(isinstance(data, dict) and data.get("schema_version") == "phase_result_v2")

    @staticmethod
    def _primary_output_key(skill: str) -> str:
        _ = skill
        return "output"

    @staticmethod
    def _extract_labeled_artifacts(response_text: str, skill: str) -> dict:
        _ = response_text
        _ = skill
        return {}

    @staticmethod
    def _backfill_launch_copy_named_outputs(artifacts: dict, response_text: str) -> None:
        if not isinstance(artifacts, dict):
            return
        if (
            str(artifacts.get("approved_subject") or "").strip()
            and str(artifacts.get("approved_body") or "").strip()
        ):
            return
        source_text = str(artifacts.get("launch_copy") or response_text or "").strip()
        if not source_text:
            return

        lines = [line.rstrip() for line in source_text.splitlines()]
        subject = ""
        body_lines: list[str] = []
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            lowered = stripped.lower()
            if lowered.startswith("subject:"):
                subject = stripped.split(":", 1)[1].strip()
                body_lines = lines[index + 1 :]
                break
        if not subject:
            first_non_empty = next((line.strip() for line in lines if line.strip()), "")
            subject = first_non_empty[:120].strip()
            body_lines = lines[1:] if len(lines) > 1 else []

        body = "\n".join(body_lines).strip()
        if not body:
            body = source_text.strip()

        if subject and not str(artifacts.get("approved_subject") or "").strip():
            artifacts["approved_subject"] = subject
        if body and not str(artifacts.get("approved_body") or "").strip():
            artifacts["approved_body"] = body


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
