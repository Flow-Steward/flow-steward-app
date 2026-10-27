# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from .prompt_agent_quality import *  # noqa: F403,F401  # NOSONAR


class PromptAgentPhaseResultMixin:
    """Agent output validation and phase_result_v2 normalization."""

    @staticmethod
    def validate_agent_output(skill: str, artifacts: dict) -> list[str]:
        """Validate agent artifacts against the output contract from the registry.

        Loads the contract for the given skill_id and checks that each
        required output field is present and non-empty in the artifacts dict.

        Returns a list of validation error strings (empty = valid).
        Non-fatal: returns empty list on any DB/import error so agents are
        never blocked by registry issues.
        """
        if not skill:
            return []

        try:
            from core.application.registry.registry_manager import RegistryManager

            rm = RegistryManager.instance()
            contract = rm.db_store.get_skill_contract(skill)
        except Exception as e:
            logger.warning(
                "validate_agent_output: could not load contract for '%s': %s",
                skill,
                e,
            )
            return []

        required = contract.get("required_fields", [])
        if not required:
            return []

        # Build a type lookup for smarter emptiness checks
        {f["name"]: f.get("type", "text") for f in contract.get("all_fields", [])}

        errors = []
        for field in required:
            value = artifacts.get(field)
            if value is None:
                errors.append(f"Missing required output: '{field}'")
            elif isinstance(value, str) and not value.strip():
                errors.append(f"Empty required output: '{field}'")
            # For json-typed fields, empty list/dict is valid (e.g. blocking_issues=[])
            # For text-typed fields, empty string already caught above
        return errors

    @staticmethod
    def _extract_phase_result_candidate(response_text: str) -> dict:
        """Extract a potential phase_result_v2 envelope from response text."""

        def _parse_candidate(raw: str) -> dict:
            if not raw:
                return {}
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                # Strict structured routes never repair malformed provider JSON.
                # The caller emits a typed failed PhaseResultV2 instead.
                return {}
            if isinstance(parsed, dict) and parsed.get("schema_version") == "phase_result_v2":
                return parsed
            return {}

        # Prefer JSON code blocks
        for raw in PromptAgent._iter_json_fence_payloads(response_text):
            if not raw:
                continue
            parsed = _parse_candidate(raw)
            if parsed:
                return parsed

        # Fallback: entire response is JSON
        stripped = (response_text or "").strip()
        if stripped.startswith("{"):
            parsed = _parse_candidate(stripped)
            if parsed:
                return parsed
        return {}

    @staticmethod
    def _iter_json_fence_payloads(response_text: str) -> list[str]:
        payloads: list[str] = []
        raw = str(response_text or "")
        cursor = 0
        fence = "```"

        while cursor < len(raw):
            start = raw.find(fence, cursor)
            if start < 0:
                break

            line_end = raw.find("\n", start + len(fence))
            if line_end < 0:
                break

            header = raw[start + len(fence) : line_end].strip().lower()
            end = raw.find(fence, line_end + 1)
            if end < 0:
                break

            if header == "json":
                payload = raw[line_end + 1 : end].strip()
                if payload:
                    payloads.append(payload)

            cursor = end + len(fence)

        return payloads

    @staticmethod
    def _coerce_list(value: Any) -> list:
        if isinstance(value, list):
            return value
        return []

    @staticmethod
    def _coerce_str_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()]

    @staticmethod
    def _normalize_verdict_value(value: Any, *, status: Any = None) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        if not normalized:
            return ""
        canonical = PromptAgent._canonical_verdict_from_text(normalized)
        if canonical:
            return canonical

        canonical_status = PromptAgent._canonical_verdict_from_text(status)
        if canonical_status == "pass":
            return "needs_review"
        if canonical_status:
            return canonical_status
        return value

    @staticmethod
    def _canonical_verdict_from_text(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        normalized = value.strip().lower()
        if not normalized:
            return ""
        compact = re.sub(r"[\s.!,;]+$", "", normalized)
        canonical_exact = {
            "pass": "pass",
            "passed": "pass",
            "fail": "fail",
            "failed": "fail",
            "approve": "approved",
            "approved": "approved",
            "reject": "rejected",
            "rejected": "rejected",
            "block": "blocked",
            "blocked": "blocked",
            "escalate": "escalate",
            "escalated": "escalate",
        }
        if compact in canonical_exact:
            return canonical_exact[compact]

        prefix_match = re.match(
            r"^(pass|passed|fail|failed|approved?|rejected?|blocked?|escalated?)\s*:", normalized
        )
        if prefix_match:
            return canonical_exact.get(prefix_match.group(1), "")

        positive_exact = (
            "no security issues",
            "no security issues found",
            "no security issue",
            "no vulnerabilities",
            "no vulnerability",
            "no blocking issues",
            "no blocking issue",
            "no issues found",
            "no findings",
        )
        if compact in positive_exact:
            return "pass"
        return ""

    @classmethod
    def _normalize_contract_artifacts(
        cls,
        artifacts: dict[str, Any],
        *,
        status: Any = None,
    ) -> dict[str, Any]:
        normalized = dict(artifacts or {})
        for key, value in normalized.items():
            if str(key).strip().endswith("_verdict"):
                normalized[key] = cls._normalize_verdict_value(value, status=status)
        return normalized

    @staticmethod
    def _verdict_review_required_fields(artifacts: dict[str, Any]) -> list[str]:
        return [
            str(key)
            for key, value in (artifacts or {}).items()
            if str(key).strip().endswith("_verdict") and str(value or "").strip() == "needs_review"
        ]

    @staticmethod
    def _next_action_for_status(status: str) -> str:
        return phase_next_action(status)

    def _build_phase_result_v2(
        self,
        response_text: str,
        artifacts: dict,
        status: str,
        confidence: float,
        contract_errors: list[str],
    ) -> tuple[dict, list[str]]:
        """Build and validate a strict PhaseResultV2 envelope with fallback synthesis."""
        candidate = self._extract_phase_result_candidate(response_text)
        explicit_envelope = bool(candidate)
        if not explicit_envelope:
            fallback_payload = {
                "schema_version": "phase_result_v2",
                "status": "fail",
                "summary": "Agent response missing explicit phase_result_v2 envelope.",
                "artifacts": dict(artifacts or {}),
                "evidence": [],
                "errors": [INVALID_PHASE_RESULT_V2_PAYLOAD],
                "files_to_touch": [],
                "next_action": "retry_phase",
                "confidence": 0.0,
            }
            fallback_model, fallback_errors = validate_phase_result_payload(fallback_payload)
            if fallback_model and not fallback_errors:
                return fallback_model.model_dump(), [INVALID_PHASE_RESULT_V2_PAYLOAD]
            return fallback_payload, (fallback_errors or [INVALID_PHASE_RESULT_V2_PAYLOAD])

        candidate_artifacts = candidate.get("artifacts")
        if not isinstance(candidate_artifacts, dict):
            candidate_artifacts = {}
        if explicit_envelope and "summary" in candidate:
            summary = str(candidate.get("summary") or "").strip()
        else:
            summary = response_text[:500] if response_text else COMPLETED_PREFIX

        candidate_status = normalize_phase_status(
            candidate.get("status") if "status" in candidate else status,
            default="",
        )
        if not explicit_envelope and not candidate_status:
            candidate_status = status

        # Runtime validation errors take precedence over model-declared status.
        if contract_errors:
            candidate_status = "fail"

        merged_artifacts = self._normalize_contract_artifacts(
            {**candidate_artifacts, **artifacts},
            status=candidate_status,
        )
        verdict_review_fields = self._verdict_review_required_fields(merged_artifacts)
        verdict_review_errors = (
            [
                "Non-canonical verdict artifact(s) require canonical pass/fail/blocked/escalate values: "
                + ", ".join(verdict_review_fields[:10])
            ]
            if verdict_review_fields
            else []
        )
        if verdict_review_errors and candidate_status == "pass":
            candidate_status = "fail"

        try:
            candidate_confidence = float(candidate.get("confidence", confidence))
        except (TypeError, ValueError):
            candidate_confidence = confidence
        candidate_confidence = max(0.0, min(1.0, candidate_confidence))

        next_action = str(
            candidate.get("next_action") if "next_action" in candidate else ""
        ).strip()
        if (
            verdict_review_errors
            or not next_action
            or (
                not explicit_envelope
                and next_action not in ("next_phase", "retry_phase", "escalate_to_human", "none")
            )
        ):
            next_action = self._next_action_for_status(candidate_status)

        files_to_touch = self._coerce_str_list(candidate.get("files_to_touch"))

        errors = self._coerce_str_list(candidate.get("errors"))
        if contract_errors:
            errors.extend(contract_errors)
        if verdict_review_errors:
            errors.extend(verdict_review_errors)

        payload = {
            "schema_version": "phase_result_v2",
            "status": candidate_status,
            "summary": summary,
            "artifacts": merged_artifacts,
            "evidence": self._coerce_list(candidate.get("evidence")),
            "errors": errors,
            "files_to_touch": files_to_touch,
            "next_action": next_action,
            "confidence": candidate_confidence,
        }

        phase_result_model, schema_errors = validate_phase_result_payload(payload)
        if phase_result_model and not schema_errors:
            return phase_result_model.model_dump(), []

        # Fail closed with a valid envelope and explicit schema errors.
        fallback_payload = {
            "schema_version": "phase_result_v2",
            "status": "fail",
            "summary": "PhaseResultV2 schema validation failed; regenerate response using the required envelope.",
            "artifacts": merged_artifacts,
            "evidence": [],
            "errors": schema_errors or [INVALID_PHASE_RESULT_V2_PAYLOAD],
            "files_to_touch": files_to_touch,
            "next_action": "retry_phase",
            "confidence": 0.0,
        }
        fallback_model, fallback_errors = validate_phase_result_payload(fallback_payload)
        if fallback_model and not fallback_errors:
            return fallback_model.model_dump(), (schema_errors or [INVALID_PHASE_RESULT_V2_PAYLOAD])
        # Defensive final fallback (should not happen)
        return fallback_payload, (
            schema_errors or fallback_errors or [INVALID_PHASE_RESULT_V2_PAYLOAD]
        )


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
