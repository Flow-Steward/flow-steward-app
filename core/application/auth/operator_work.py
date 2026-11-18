"""The writes a person with the operator role may make (ADR 0066 amendment, ADR 0075).

An admin configures the deployment, an operator does the work the admin configured,
and a viewer reads. Every write a person makes requires the admin role unless it is
listed here. The list is the same for a browser session and for the person's own
tokens (personal access tokens, agent access, OAuth clients such as MCP): one rule
for every channel. Service accounts keep their own rule, narrowed by token scopes.
"""

from __future__ import annotations

import re

_WRITE = r"[^/]+"

#: (method, exact path pattern). Near-miss paths are not operator work.
_OPERATOR_WORK_WRITES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Starting a workflow: the browser launch route and the API/MCP launch route.
    ("POST", re.compile(rf"^/api/workflows/{_WRITE}/run$")),
    ("POST", re.compile(r"^/api/workflow-runs$")),
    # Reviewing and steering runs.
    ("POST", re.compile(rf"^/api/approvals/{_WRITE}$")),
    (
        "POST",
        re.compile(
            rf"^/api/jobs/{_WRITE}/"
            r"(?:cancel|restart|retry-step|acknowledge|unacknowledge|decision-overrides)$"
        ),
    ),
    # Reading out results through routes the admin configured.
    ("POST", re.compile(r"^/api/reports/generate$")),
    ("POST", re.compile(rf"^/api/projects/{_WRITE}/notifications/dispatch$")),
    ("POST", re.compile(rf"^/api/projects/{_WRITE}/workflow-preview$")),
    # Secret values (ADR 0057): create, replace a value, edit metadata. Binding,
    # unbinding, approving a configuration and deleting are the admin's.
    ("POST", re.compile(r"^/api/secrets$")),
    ("PATCH", re.compile(rf"^/api/secrets/{_WRITE}$")),
    ("PUT", re.compile(rf"^/api/secrets/{_WRITE}/value$")),
    # The person's own account, whatever the authentication source.
    ("POST", re.compile(r"^/api/auth/(?:logout|change-password|switch-account|tokens)$")),
    ("POST", re.compile(rf"^/api/auth/tokens/{_WRITE}/revoke$")),
    ("POST", re.compile(r"^/api/auth/agent-access/tokens$")),
    ("POST", re.compile(rf"^/api/auth/oauth-grants/{_WRITE}/revoke$")),
    ("POST", re.compile(rf"^/api/oauth/authorizations/{_WRITE}/decision$")),
    ("POST", re.compile(r"^/api/users/me/(?:email-change/request|detect-timezone)$")),
    ("PATCH", re.compile(r"^/api/users/me/(?:profile|timezone|locale|date-time-format)$")),
)

#: Extension page actions and queries (ADR 0067) and resolving an external effect:
#: work on a page an extension offers, with the project's connection.
_EXTENSION_WORK_WRITES: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^/api/extensions/{_WRITE}/action/{_WRITE}$"),
    re.compile(rf"^/api/extensions/{_WRITE}/query/{_WRITE}$"),
    re.compile(
        rf"^/api/projects/{_WRITE}/extensions/{_WRITE}/external-effects/{_WRITE}/resolution$"
    ),
)


def is_operator_work_action(path: str, method: str) -> bool:
    """Whether a person with the operator role may make this write."""
    path_text = str(path or "").strip()
    method_text = str(method or "").strip().upper()
    if method_text == "POST" and any(
        pattern.fullmatch(path_text) for pattern in _EXTENSION_WORK_WRITES
    ):
        return True
    return any(
        method_text == write_method and pattern.fullmatch(path_text)
        for write_method, pattern in _OPERATOR_WORK_WRITES
    )


__all__ = ["is_operator_work_action"]
