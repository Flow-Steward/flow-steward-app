"""ADR 0047 decision 12: one fail-closed classification of auth sources (task5b F1, F2).

A new ``auth_source`` value must be classified as token-scoped or role-authorized;
otherwise it would silently receive role authority. The guard scans ``core/``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.application.auth.principal import (
    ROLE_AUTHORIZED_AUTH_SOURCES,
    TOKEN_SCOPED_AUTH_SOURCES,
    is_token_scoped_principal,
)

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "core"
PRINCIPAL = CORE / "application/auth/principal.py"
_TOKEN_LITERALS = {"pat", "machine"}
_SOURCE_KEYWORDS = {"auth_source", "default_auth_source"}


def _string_constants(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _string_constants(node.body) + _string_constants(node.orelse)
    return []


def _token_set_literals(tree: ast.AST) -> list[int]:
    """Lines holding a set/tuple/list literal of auth sources such as {"pat", "machine"}."""
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
            values = {item.value for item in node.elts if isinstance(item, ast.Constant)}
            if values & _TOKEN_LITERALS and values <= _TOKEN_LITERALS | {"token", "oauth"}:
                lines.append(node.lineno)
    return lines


def _assigned_auth_sources(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in _SOURCE_KEYWORDS:
            found.extend((node.value.lineno, value) for value in _string_constants(node.value))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "auth_source":
                    found.extend((value.lineno, item) for item in _string_constants(value))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {
                target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
                for target in targets
            }
            if names & _SOURCE_KEYWORDS and node.value is not None:
                found.extend((node.lineno, item) for item in _string_constants(node.value))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setdefault"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "auth_source"
        ):
            found.extend((node.lineno, item) for item in _string_constants(node.args[1]))
    return found


def _violations(source: str, *, path: str, allow_token_sets: bool) -> list[str]:
    tree = ast.parse(source)
    classified = TOKEN_SCOPED_AUTH_SOURCES | ROLE_AUTHORIZED_AUTH_SOURCES
    problems = [
        f"{path}:{line}: unclassified auth_source {value!r}"
        for line, value in _assigned_auth_sources(tree)
        if value.strip().lower() not in classified
    ]
    if not allow_token_sets:
        problems.extend(
            f"{path}:{line}: hard-coded token auth-source set; use is_token_scoped_principal"
            for line in _token_set_literals(tree)
        )
    return problems


def test_every_auth_source_in_core_is_classified_and_no_token_set_is_copied() -> None:
    problems: list[str] = []
    scanned = 0
    for path in sorted(CORE.rglob("*.py")):
        scanned += 1
        problems.extend(
            _violations(
                path.read_text(encoding="utf-8"),
                path=str(path.relative_to(ROOT)),
                allow_token_sets=path == PRINCIPAL,
            )
        )
    assert scanned > 1000  # non-vacuity: the real tree
    assert problems == []


def test_the_guard_fails_on_a_synthetic_unclassified_source_and_a_copied_set() -> None:
    source = (
        "principal = {'auth_source': 'agent_link'}\n"
        "user.setdefault('auth_source', 'bridge')\n"
        "ScopeContext(auth_source='robot')\n"
        "def f(source):\n"
        "    return source in {'pat', 'machine', 'oauth'}\n"
    )
    problems = _violations(source, path="synthetic.py", allow_token_sets=False)
    assert len(problems) == 4
    assert any("'agent_link'" in item for item in problems)
    assert any("'bridge'" in item for item in problems)
    assert any("'robot'" in item for item in problems)
    assert any("hard-coded token" in item for item in problems)


def test_the_classification_is_disjoint_and_knows_oauth() -> None:
    assert not TOKEN_SCOPED_AUTH_SOURCES & ROLE_AUTHORIZED_AUTH_SOURCES
    assert {"pat", "machine", "token", "oauth"} == TOKEN_SCOPED_AUTH_SOURCES
    assert is_token_scoped_principal({"auth_source": "oauth"})
    assert is_token_scoped_principal({"auth_source": "local", "principal_type": "service_account"})
    assert not is_token_scoped_principal({"auth_source": "local"})
    assert not is_token_scoped_principal(None)


# ---------------------------------------------------------------------------
# F2: each of the eight places of decision 12 with an OAuth principal
# ---------------------------------------------------------------------------
_ACCOUNT = "00000000-0000-0000-0000-000000000002"
_OAUTH_ADMIN = {
    "id": "user-1",
    "username": "admin",
    "principal_type": "admin",
    "roles": ["admin"],
    "auth_source": "oauth",
    "account_id": "00000000-0000-0000-0000-000000000002",
    "scopes": ["projects.read", "jobs.read"],
}


def test_place_1_http_scope_check_applies_to_oauth() -> None:
    from core.application.auth.auth_http_runtime import _principal_requires_http_scopes

    assert _principal_requires_http_scopes(dict(_OAUTH_ADMIN))


def test_place_2_token_ceiling_comes_from_oauth_scopes_not_the_admin_role() -> None:
    from core.application.auth.principal import (
        TokenAuthorityError,
        _caller_is_scope_restricted,
        enforce_token_scope_within_caller_authority,
    )

    assert _caller_is_scope_restricted(dict(_OAUTH_ADMIN))
    with pytest.raises(TokenAuthorityError):
        enforce_token_scope_within_caller_authority(
            dict(_OAUTH_ADMIN),
            requested_scopes=["admin:all"],
            requested_project_ids=[],
            requested_workflow_ids=[],
        )


def test_place_3_oauth_principal_without_account_is_refused() -> None:
    """F4: never the default account."""
    from core.application.auth.resolve import normalize_principal

    without_account = {key: value for key, value in _OAUTH_ADMIN.items() if key != "account_id"}
    assert normalize_principal(without_account) is None
    normalized = normalize_principal(
        {**_OAUTH_ADMIN, "oauth_grant_id": "g", "oauth_client_id": "c", "oauth_resource": "r"}
    )
    assert normalized is not None
    assert (normalized.oauth_grant_id, normalized.oauth_client_id, normalized.oauth_resource) == (
        "g",
        "c",
        "r",
    )
    assert normalized.auth_source == "oauth"


def test_place_5_event_stream_treats_oauth_as_a_token() -> None:
    from core.entrypoints.api.events_stream import _is_external_principal

    assert _is_external_principal(dict(_OAUTH_ADMIN))
    assert not _is_external_principal({"auth_source": "local", "principal_type": "admin"})


def test_place_6_jobs_use_the_token_approval_gate_for_oauth() -> None:
    from core.application.management.scope import ScopeContext
    from core.entrypoints.api.jobs import _is_token_scope

    assert _is_token_scope(ScopeContext(account_id=_ACCOUNT, auth_source="oauth"))
    assert not _is_token_scope(ScopeContext(account_id=_ACCOUNT, auth_source="local"))


def test_place_7_approval_decision_of_oauth_is_a_token_decision() -> None:
    from core.application.management.command_models_api_inputs import ApprovalDecisionInput
    from core.application.management.command_services_approval import ApprovalCommandService
    from core.application.management.errors import ServiceValidationError
    from core.application.management.scope import ScopeContext

    service = ApprovalCommandService(
        resume_shutdown_in_progress=lambda: False,
        get_checkpointer=lambda: None,
        get_logger=lambda: None,
        enqueue_workflow_resume=lambda **_kwargs: None,
        workflow_resume_repository_factory=lambda _bl: None,
    )
    decision = ApprovalDecisionInput.model_validate(
        {"approved": False, "action": "approve", "approval_id": "a-1"}
    )
    with pytest.raises(ServiceValidationError, match="approved conflicts with approve action"):
        service.resume_task(
            thread_id="t-1",
            scope=ScopeContext(account_id=_OAUTH_ADMIN["account_id"], auth_source="oauth"),
            decision=decision,
        )


def test_place_8_approval_queries_treat_oauth_as_a_token() -> None:
    from core.application.management.query_services_approval import _is_token_principal
    from core.application.management.scope import ScopeContext

    assert _is_token_principal(ScopeContext(account_id=_ACCOUNT, auth_source="oauth"))
    assert not _is_token_principal(ScopeContext(account_id=_ACCOUNT, auth_source="external"))
