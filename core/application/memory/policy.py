from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .exceptions import MemoryPolicyError
from .models import (
    CONFIRMATION_REQUIRED_TYPES,
    LIFECYCLE_STATUSES,
    MEMORY_TYPES,
    SOURCE_TRUST_LEVELS,
    VISIBILITIES,
    MemoryRecordInput,
    MemorySearchFilters,
    ScopeId,
)


@dataclass(frozen=True, slots=True)
class MemorySafetyFinding:
    rule_id: str
    category: str
    source_field: str
    snippet_class: str


_SECRET_FIELD_NAMES = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "id_token",
        "auth_token",
        "bearer_token",
        "authorization",
        "secret_key",
        "client_secret",
        "api_secret",
        "webhook_secret",
        "signing_secret",
        "aws_secret_access_key",
        "private_key",
    }
)
_SECRET_ASSIGNMENT_NAMES = (
    "password",
    "passwd",
    "pwd",
    "api[_\\-\\s]?key",
    "access[_\\-\\s]?token",
    "refresh[_\\-\\s]?token",
    "id[_\\-\\s]?token",
    "auth[_\\-\\s]?token",
    "bearer[_\\-\\s]?token",
    "authorization",
    "secret[_\\-\\s]?key",
    "client[_\\-\\s]?secret",
    "api[_\\-\\s]?secret",
    "webhook[_\\-\\s]?secret",
    "signing[_\\-\\s]?secret",
    "aws[_\\-\\s]?secret[_\\-\\s]?access[_\\-\\s]?key",
)
_SECRET_ASSIGNMENT_PATTERNS = tuple(
    re.compile(rf"""(?ix)\b{name}\s*[:=]\s*["']?([^\s"',;]{{8,}})""")
    for name in _SECRET_ASSIGNMENT_NAMES
)
_AUTHORIZATION_HEADER_RE = re.compile(r"(?im)^\s*authorization\s*:\s*(bearer|basic)\s+[^\s]+")
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{16,}")
_OPENAI_STYLE_KEY_RE = re.compile(r"\bsk-(?a:[\w-]){20,}\b")
_AWS_ACCESS_KEY_RE = re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PROMPT_INJECTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bignore\s+(?:all\s+)?previous\s+instructions\b",
        r"\breveal\s+(?:the\s+)?(?:hidden\s+)?(?:system|developer)\s+prompt\b",
        r"\bprint\s+(?:the\s+)?(?:system|developer)\s+prompt\b",
        r"\bdisclose\s+(?:the\s+)?(?:hidden\s+)?prompt\b",
        r"\bbypass\s+(?:the\s+)?(?:system|developer)\s+instructions\b",
    )
)

_TRUST_RANK = {
    "canonical_doc": 6,
    "human_approved": 5,
    "tool_observed": 4,
    "system_generated": 3,
    "user_supplied": 2,
    "agent_inferred": 1,
}


def normalize_alias(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def validate_record_input(payload: MemoryRecordInput) -> None:
    if payload.memory_type not in MEMORY_TYPES:
        raise MemoryPolicyError(f"Unsupported memory_type: {payload.memory_type}")
    if payload.visibility not in VISIBILITIES:
        raise MemoryPolicyError(f"Unsupported visibility: {payload.visibility}")
    if payload.lifecycle_status not in LIFECYCLE_STATUSES:
        raise MemoryPolicyError(f"Unsupported lifecycle_status: {payload.lifecycle_status}")
    if payload.source_trust not in SOURCE_TRUST_LEVELS:
        raise MemoryPolicyError(f"Unsupported source_trust: {payload.source_trust}")
    if not str(payload.account_id or "").strip():
        raise MemoryPolicyError("account_id is required")
    if not str(payload.created_by or "").strip():
        raise MemoryPolicyError("created_by is required")
    if not str(payload.title or "").strip():
        raise MemoryPolicyError("title is required")
    if not str(payload.summary or payload.content or "").strip():
        raise MemoryPolicyError("summary or content is required")


def enforce_human_confirmation(payload: MemoryRecordInput) -> MemoryRecordInput:
    if payload.source_trust in {"human_approved", "canonical_doc"}:
        return payload
    metadata = dict(payload.metadata or {})
    if payload.memory_type in CONFIRMATION_REQUIRED_TYPES or bool(
        metadata.get("security_sensitive")
    ):
        payload.lifecycle_status = "candidate"
    return payload


def enforce_memory_safety(payload: MemoryRecordInput) -> MemoryRecordInput:
    finding = first_memory_safety_finding(payload)
    if finding is not None:
        diagnostics = memory_safety_diagnostics(finding)
        message = (
            "Unsafe prompt-injected content cannot be indexed into memory"
            if finding.category == "prompt_injection"
            else "Sensitive content cannot be indexed into curated memory"
        )
        raise MemoryPolicyError(f"{message}: {diagnostics}", diagnostics=diagnostics)
    return payload


def _payload_text_fields(payload: MemoryRecordInput) -> tuple[tuple[str, str], ...]:
    return (
        ("title", str(payload.title or "")),
        ("summary", str(payload.summary or "")),
        ("content", str(payload.content or "")),
        ("tags", " ".join(str(tag or "") for tag in (payload.tags or []))),
    )


def _field_name_findings(payload: MemoryRecordInput) -> MemorySafetyFinding | None:
    metadata = payload.metadata if isinstance(payload.metadata, Mapping) else {}
    for key in metadata:
        key_text = str(key or "")
        normalized_key = re.sub(r"[_\-\s]+", "_", key_text.casefold()).strip("_")
        if normalized_key in _SECRET_FIELD_NAMES:
            return MemorySafetyFinding(
                rule_id="secret:sensitive_field_name",
                category="secret",
                source_field=f"metadata.{key_text}",
                snippet_class="sensitive_field_name",
            )
    return None


def first_memory_safety_finding(payload: MemoryRecordInput) -> MemorySafetyFinding | None:
    field_finding = _field_name_findings(payload)
    if field_finding is not None:
        return field_finding
    for source_field, text in _payload_text_fields(payload):
        if not text:
            continue
        checks = (
            (
                "secret:authorization_header",
                "secret",
                "authorization_header",
                _AUTHORIZATION_HEADER_RE,
            ),
            ("secret:bearer_token", "secret", "bearer_token", _BEARER_TOKEN_RE),
            ("secret:openai_style_key", "secret", "api_key", _OPENAI_STYLE_KEY_RE),
            ("secret:aws_access_key", "secret", "api_key", _AWS_ACCESS_KEY_RE),
            ("secret:private_key", "secret", "private_key", _PRIVATE_KEY_RE),
            ("secret:ssn", "secret", "ssn", _SSN_RE),
            *(
                ("secret:assignment", "secret", "secret_assignment", pattern)
                for pattern in _SECRET_ASSIGNMENT_PATTERNS
            ),
            *(
                (
                    "prompt_injection:instruction_override",
                    "prompt_injection",
                    "instruction_override",
                    pattern,
                )
                for pattern in _PROMPT_INJECTION_PATTERNS
            ),
        )
        for rule_id, category, snippet_class, pattern in checks:
            if pattern.search(text):
                return MemorySafetyFinding(
                    rule_id=rule_id,
                    category=category,
                    source_field=source_field,
                    snippet_class=snippet_class,
                )
    return None


def memory_safety_diagnostics(finding: MemorySafetyFinding) -> dict[str, str]:
    return {
        "rule_id": finding.rule_id,
        "category": finding.category,
        "source_field": finding.source_field,
        "matched": finding.snippet_class,
    }


def trust_rank(source_trust: str) -> int:
    return _TRUST_RANK.get(str(source_trust or "").strip(), 0)


def _tuple_or_empty(values: Iterable[str] | None) -> tuple[str, ...]:
    normalized = []
    seen = set()
    for raw_value in values or ():
        value = str(raw_value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return tuple(normalized)


def _scope_id(value: Any) -> ScopeId | None:
    normalized = str(value or "").strip()
    return normalized or None


class MemoryPolicyResolver:
    def resolve_filters(
        self,
        *,
        account_id: str,
        actor_id: str = "",
        tenant_id: str | None = None,
        project_id: ScopeId | None = None,
        workflow_id: str | None = None,
        job_id: ScopeId | None = None,
        session_id: str | None = None,
        memory_types: Iterable[str] | None = None,
        lifecycle_statuses: Iterable[str] | None = None,
        source_trusts: Iterable[str] | None = None,
        tags: Iterable[str] | None = None,
        source_types: Iterable[str] | None = None,
        limit: int = 10,
        offset: int = 0,
        include_private_user: bool = False,
        project_scope_strict: bool = False,
        prefer_active: bool = True,
    ) -> MemorySearchFilters:
        if not str(account_id or "").strip():
            raise MemoryPolicyError("account_id is required")

        visibilities = ["system", "account"]
        if tenant_id:
            visibilities.append("tenant")
        if project_id is not None:
            visibilities.append("project")
        if include_private_user and actor_id:
            visibilities.append("private_user")

        normalized_memory_types = _tuple_or_empty(memory_types)
        unsupported_types = [item for item in normalized_memory_types if item not in MEMORY_TYPES]
        if unsupported_types:
            raise MemoryPolicyError(f"Unsupported memory types: {unsupported_types}")

        normalized_statuses = _tuple_or_empty(lifecycle_statuses) or ("active", "candidate")
        unsupported_statuses = [
            item for item in normalized_statuses if item not in LIFECYCLE_STATUSES
        ]
        if unsupported_statuses:
            raise MemoryPolicyError(f"Unsupported lifecycle statuses: {unsupported_statuses}")

        normalized_trusts = _tuple_or_empty(source_trusts)
        unsupported_trusts = [item for item in normalized_trusts if item not in SOURCE_TRUST_LEVELS]
        if unsupported_trusts:
            raise MemoryPolicyError(f"Unsupported source trust filters: {unsupported_trusts}")

        return MemorySearchFilters(
            account_id=str(account_id).strip(),
            actor_id=str(actor_id or "").strip(),
            tenant_id=str(tenant_id or "").strip() or None,
            project_id=_scope_id(project_id),
            project_scope_strict=bool(project_scope_strict),
            workflow_id=str(workflow_id or "").strip() or None,
            job_id=_scope_id(job_id),
            session_id=str(session_id or "").strip() or None,
            memory_types=normalized_memory_types,
            lifecycle_statuses=normalized_statuses,
            visibilities=tuple(visibilities),
            source_trusts=normalized_trusts,
            tags=_tuple_or_empty(tags),
            source_types=_tuple_or_empty(source_types),
            include_private_user=bool(include_private_user and actor_id),
            prefer_active=prefer_active,
            prefer_non_superseded=True,
            only_safe=True,
            limit=max(1, min(int(limit or 10), 100)),
            offset=max(0, int(offset or 0)),
        )

    def filter_scope(
        self,
        *,
        account_id: str,
        tenant_id: str | None,
        project_id: ScopeId | None,
        actor_id: str,
        memory_policy: Mapping[str, Any] | None,
        categories: Iterable[str] | None = None,
    ) -> MemorySearchFilters:
        requested_types = list((memory_policy or {}).get("memory_types") or [])
        requested_statuses = list((memory_policy or {}).get("lifecycle_statuses") or [])
        requested_trusts = list((memory_policy or {}).get("source_trusts") or [])
        include_private = bool((memory_policy or {}).get("include_private_user"))
        if categories and "project_profile_context" in set(categories):
            requested_types.extend(["docs_note"])
        return self.resolve_filters(
            account_id=account_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
            project_id=project_id,
            memory_types=requested_types,
            lifecycle_statuses=requested_statuses or ("active", "candidate"),
            source_trusts=requested_trusts,
            include_private_user=include_private,
            limit=int((memory_policy or {}).get("limit") or 10),
        )
