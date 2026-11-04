"""Stored secrets chosen for an LLM provider install's secrets (ADR 0057).

An install keeps the values typed into its secret fields in one encrypted blob
(``llm_provider_credential_secret_ref``). A field may instead use a secret from
Registry → Secrets: the install then keeps ``{secret name: storage key}`` under
``llm_provider_catalog_refs`` in its ``config_jsonb``, written together with the
``catalog_secret_bindings`` row, and reads the value behind the key at every use.
A value replaced in Registry → Secrets therefore reaches the provider at its next
call; nothing is copied.

Every reader of the install's credentials goes through :func:`effective_credentials`,
which overlays the chosen values on the typed ones and fails closed when a chosen
value cannot be read. Values never leave the host and are never returned to a
browser: the status lists names and the chosen secret's id and name only.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from typing import Any

CATALOG_REFS_KEY = "llm_provider_catalog_refs"
CONSUMER_KIND = "llm_provider"
UNAVAILABLE_DETAIL = "Stored extension credentials are unavailable"


class StoredCredentialsUnavailableError(Exception):
    """A saved or chosen credential cannot be read. The message never holds a value."""

    def __init__(self, detail: str = UNAVAILABLE_DETAIL) -> None:
        super().__init__(detail)
        self.detail = detail


def _text(value: Any) -> str:
    return str(value or "").strip()


def refs_value(value: Any) -> dict[str, str]:
    """The ``{secret name: storage key}`` map, from a jsonb value or its text."""
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except ValueError:
            return {}
    if not isinstance(value, dict):
        return {}
    return {_text(name): _text(key) for name, key in value.items() if _text(name) and _text(key)}


def catalog_refs_from(install_config: Any) -> dict[str, str]:
    config = install_config if isinstance(install_config, dict) else {}
    return refs_value(config.get(CATALOG_REFS_KEY))


def read_catalog_refs(conn: Any, *, account_id: str, install_id: str) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT config_jsonb -> 'llm_provider_catalog_refs'
              FROM extension_installs
             WHERE account_id = %s AND install_id = %s
            """,
            (_text(account_id), _text(install_id)),
        )
        row = cur.fetchone()
    if not row:
        return {}
    value = next(iter(row.values()), None) if isinstance(row, dict) else row[0]
    return refs_value(value)


def write_catalog_refs(cur: Any, *, account_id: str, install_id: str, refs: dict[str, str]) -> None:
    """Replace the install's map; an empty map removes the key."""
    cleaned = refs_value(refs)
    if cleaned:
        cur.execute(
            """
            UPDATE extension_installs
               SET config_jsonb = jsonb_set(
                       COALESCE(config_jsonb, '{}'::jsonb),
                       '{llm_provider_catalog_refs}',
                       %s::jsonb,
                       true
                   )
             WHERE account_id = %s AND install_id = %s
            """,
            (json.dumps(cleaned, sort_keys=True), _text(account_id), _text(install_id)),
        )
    else:
        cur.execute(
            """
            UPDATE extension_installs
               SET config_jsonb = COALESCE(config_jsonb, '{}'::jsonb) - 'llm_provider_catalog_refs'
             WHERE account_id = %s AND install_id = %s
            """,
            (_text(account_id), _text(install_id)),
        )


class _OnCursor:
    """Lets the repository helpers that open their own cursor run on a given one."""

    def __init__(self, cur: Any) -> None:
        self._cur = cur

    def cursor(self, **_kwargs: Any) -> Any:
        return contextlib.nullcontext(self._cur)


def release_catalog_ref(cur: Any, *, account_id: str, install_id: str, secret_name: str) -> None:
    """A confirmed delete of the chosen secret: the install stops referencing it.

    The typed-value blob is left alone. What was checked against the deleted value
    no longer describes the install, so the last check is cleared, the state
    revision changes (an in-flight discovery cannot commit) and verified models
    must be verified again.
    """
    from core.application.settings import llm_provider_extension_repository as store

    name = _text(secret_name)
    cur.execute(
        """
        UPDATE extension_installs
           SET config_jsonb = CASE
                   WHEN (COALESCE(config_jsonb -> 'llm_provider_catalog_refs', '{}'::jsonb) - %s)
                        = '{}'::jsonb
                   THEN COALESCE(config_jsonb, '{}'::jsonb) - 'llm_provider_catalog_refs'
                   ELSE jsonb_set(
                       COALESCE(config_jsonb, '{}'::jsonb),
                       '{llm_provider_catalog_refs}',
                       (config_jsonb -> 'llm_provider_catalog_refs') - %s,
                       true
                   )
               END - 'llm_provider_last_check'
         WHERE account_id = %s AND install_id = %s
        """,
        (name, name, _text(account_id), _text(install_id)),
    )
    on_cursor = _OnCursor(cur)
    store.set_install_provider_state_revision(
        on_cursor, account_id=account_id, extension_install_id=install_id, revision=uuid.uuid4().hex
    )
    store.mark_install_models_stale(
        on_cursor,
        account_id=account_id,
        extension_install_id=install_id,
        reason="credentials_changed",
    )


def record_unbind_audit(cur: Any, scope: Any, *, install_id: str, field: str) -> None:
    """The ``secret.unbind`` audit row for a stored secret this install stopped using."""
    from core.application.secrets import repository
    from core.application.secrets.authority import actor_identity

    kind, actor = actor_identity(scope)
    repository.record_audit(
        cur,
        account_id=scope.account_id,
        command_type="secret.unbind",
        actor_kind=kind,
        actor_id=actor,
        target={"consumer_kind": CONSUMER_KIND, "consumer_id": _text(install_id), "field": field},
        payload={},
    )


def chosen_secrets(conn: Any, *, account_id: str, install_id: str) -> dict[str, dict[str, str]]:
    """secret name -> the chosen catalog secret's id and name (never its value)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.field_name, s.id::text AS secret_id, s.name
              FROM catalog_secret_bindings AS b
              JOIN catalog_secrets AS s ON s.id = b.secret_id
             WHERE b.consumer_kind = 'llm_provider'
               AND b.llm_provider_install_id = %s
               AND b.account_id::text = %s
            """,
            (_text(install_id), _text(account_id)),
        )
        rows = cur.fetchall() or []
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        if isinstance(row, dict):
            field, secret_id, name = row.get("field_name"), row.get("secret_id"), row.get("name")
        else:
            field, secret_id, name = row[0], row[1], row[2]
        if _text(field) and _text(secret_id):
            result[_text(field)] = {"id": _text(secret_id), "name": _text(name)}
    return result


def _decode_blob(ciphertext: str) -> dict[str, Any]:
    from core.application.auth.secrets import decrypt_secret

    try:
        decoded = json.loads(decrypt_secret(ciphertext))
    except Exception as exc:
        raise StoredCredentialsUnavailableError() from exc
    if not isinstance(decoded, dict):
        raise StoredCredentialsUnavailableError()
    return dict(decoded)


def effective_credentials(
    conn: Any,
    *,
    account_id: str,
    ciphertext: str | None,
    catalog_refs: Any,
) -> dict[str, Any]:
    """The credentials an install uses now: typed values, then each chosen stored value.

    A chosen value is read through the host's stored-secret reader by its storage
    key, account-scoped, at every call. Any value that cannot be read fails closed.
    """
    credentials = _decode_blob(ciphertext) if ciphertext else {}
    refs = refs_value(catalog_refs)
    if not refs:
        return credentials
    from core.application.settings.llm_provider_extension_service import llm_field_label
    from core.infrastructure.security import stored_secrets

    reader = stored_secrets.stored_secret_reader(conn)
    context = stored_secrets.SecretReadContext.for_account(account_id)
    for name, ref_key in sorted(refs.items()):
        try:
            credentials[name] = reader.read(context, ref_key)
        except stored_secrets.StoredSecretError as exc:
            raise StoredCredentialsUnavailableError(
                f"The stored secret chosen for {llm_field_label(name)} is unavailable"
            ) from exc
    return credentials


__all__ = [
    "CATALOG_REFS_KEY",
    "CONSUMER_KIND",
    "UNAVAILABLE_DETAIL",
    "StoredCredentialsUnavailableError",
    "catalog_refs_from",
    "chosen_secrets",
    "effective_credentials",
    "read_catalog_refs",
    "record_unbind_audit",
    "refs_value",
    "release_catalog_ref",
    "write_catalog_refs",
]
