"""Agents and projects that route to an LLM provider install's models.

Uninstalling an LLM provider extension deletes its provider sources, so every
agent whose model names one of them, and every project whose default (or
per-agent override) names one, stops being able to run. The uninstall plan
lists them by name, beside the other objects that will stop working.
"""

from __future__ import annotations

from typing import Any


def collect_llm_model_dependents(
    bl: Any,
    *,
    install_id: str,
    account_id: str,
    include_consumer_accounts: bool,
) -> list[dict[str, Any]]:
    table_exists = getattr(bl, "_table_exists", lambda *_args: False)
    if not table_exists("llm_provider_sources"):
        return []
    cursor_factory = _cursor_factory(bl)
    if cursor_factory is None:
        return []
    with cursor_factory() as cur:
        sources = _provider_sources(
            cur,
            install_id=install_id,
            account_id=account_id,
            include_consumer_accounts=include_consumer_accounts,
        )
        if not sources:
            return []
        keys = sorted(sources)
        agents = _agent_rows(cur, keys) if table_exists("registry_agents") else []
        projects = _project_rows(cur, keys) if table_exists("projects") else []
    return [
        *(_affected_agent(row, sources) for row in agents),
        *(_affected_project(row, sources) for row in projects),
    ]


def _provider_sources(
    cur: Any, *, install_id: str, account_id: str, include_consumer_accounts: bool
) -> dict[str, str]:
    cur.execute(
        """
        SELECT provider_key, COALESCE(display_label, provider_key) AS display_label
          FROM llm_provider_sources
         WHERE extension_install_id = %s
           AND (%s OR account_id::text = %s)
        """,
        (install_id, include_consumer_accounts, account_id),
    )
    rows = [_row(item, cur.description) for item in (cur.fetchall() or [])]
    return {
        _text(row.get("provider_key")): _text(row.get("display_label"))
        for row in rows
        if _text(row.get("provider_key"))
    }


def _agent_rows(cur: Any, keys: list[str]) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT agent_id,
               COALESCE(NULLIF(display_name, ''), agent_id) AS display_name,
               COALESCE(account_id::text, '') AS account_id,
               config->>'provider' AS provider_key,
               COALESCE(config->>'model', '') AS model_id
          FROM registry_agents
         WHERE deleted_at IS NULL
           AND config->>'provider' = ANY(%s)
         ORDER BY display_name, agent_id
        """,
        (keys,),
    )
    return [_row(item, cur.description) for item in (cur.fetchall() or [])]


def _project_rows(cur: Any, keys: list[str]) -> list[dict[str, Any]]:
    # A project names a provider as its default, or for one agent through the
    # per-agent override map; either stops resolving once the source is gone.
    cur.execute(
        """
        SELECT p.id::text AS project_id,
               COALESCE(NULLIF(p.name, ''), p.id::text) AS display_name,
               COALESCE(p.account_id::text, '') AS account_id,
               COALESCE(
                   NULLIF(p.agents_config->>'default_provider', ''),
                   NULLIF(p.agents_config->>'model_provider_id', ''),
                   (SELECT o.value->>'provider'
                      FROM jsonb_each(
                          CASE WHEN jsonb_typeof(p.agents_config->'agents'->'config') = 'object'
                               THEN p.agents_config->'agents'->'config'
                               ELSE '{}'::jsonb END
                      ) AS o
                     WHERE o.value->>'provider' = ANY(%s)
                     LIMIT 1)
               ) AS provider_key,
               COALESCE(p.agents_config->>'default_model', '') AS model_id
          FROM projects p
         WHERE p.deleted_at IS NULL
           AND jsonb_typeof(p.agents_config) = 'object'
           AND (
               p.agents_config->>'default_provider' = ANY(%s)
               OR p.agents_config->>'model_provider_id' = ANY(%s)
               OR EXISTS (
                   SELECT 1
                     FROM jsonb_each(
                         CASE WHEN jsonb_typeof(p.agents_config->'agents'->'config') = 'object'
                              THEN p.agents_config->'agents'->'config'
                              ELSE '{}'::jsonb END
                     ) AS o
                    WHERE o.value->>'provider' = ANY(%s)
               )
           )
         ORDER BY display_name
        """,
        (keys, keys, keys, keys),
    )
    return [_row(item, cur.description) for item in (cur.fetchall() or [])]


def _model_label(row: dict[str, Any], sources: dict[str, str]) -> str:
    provider = sources.get(_text(row.get("provider_key"))) or _text(row.get("provider_key"))
    model = _text(row.get("model_id"))
    return f"{model} ({provider})" if model else provider


def _affected_agent(row: dict[str, Any], sources: dict[str, str]) -> dict[str, Any]:
    return {
        "kind": "agent",
        "id": _text(row.get("agent_id")),
        "display_name": _text(row.get("display_name")),
        "owner_account_id": _text(row.get("account_id")),
        # Informational: the row stays as it is; uninstall is not blocked by it.
        "action": "stops_working",
        "reason": (
            f"Uses the model {_model_label(row, sources)}; the agent can't run until it "
            "is given another model."
        ),
    }


def _affected_project(row: dict[str, Any], sources: dict[str, str]) -> dict[str, Any]:
    return {
        "kind": "project",
        "id": _text(row.get("project_id")),
        "display_name": _text(row.get("display_name")),
        "owner_account_id": _text(row.get("account_id")),
        # Informational: the row stays as it is; uninstall is not blocked by it.
        "action": "stops_working",
        "reason": (
            f"Its default model is {_model_label(row, sources)}; runs in this project fail "
            "until another default model is chosen."
        ),
    }


def _cursor_factory(bl: Any) -> Any:
    factory = getattr(bl, "cursor", None)
    if not callable(factory):
        factory = getattr(getattr(bl, "conn", None), "cursor", None)
    return factory if callable(factory) else None


def _row(item: Any, description: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    names = [column[0] for column in description or []]
    return dict(zip(names, item, strict=False))


def _text(value: Any) -> str:
    return str(value or "").strip()


__all__ = ["collect_llm_model_dependents"]
