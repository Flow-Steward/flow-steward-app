"""Establish the long-lived secrets a runtime nobody installed has to have.

AC17-R121, and the natural twin of AC17-R117. That one taught the runtime to mint
its own one-time setup link; this one is the same gap for the secrets that are not
one-time, because both were things only the installer could do. A Compact
installation started from the image alone comes up, lets an administrator in through
the self-minted link, and fails on the first action it is asked to perform:

    ERROR [secret_codec] ENCRYPTION_KEY is not set; encrypted secret storage is
    unavailable.

Which secrets belong here is derived, not listed. The environment registry already
knows every leaf secret the installer generates, and the supervisor's allowlists
already decide which variables a Compact role may inherit; a secret in both is one
this runtime's own processes read and nobody is going to hand it. Two are removed
from that intersection by name because an external service owns them: a minted
POSTGRES_PASSWORD or RABBITMQ_PASS would not authenticate against anything, and on
this profile the operator supplies both inside the connection URLs. FS_BACKUP_
ENCRYPTION_KEY falls out of the derivation by itself -- no role inherits it, because
backups run in a separate one-shot container -- and it is left out rather than
special-cased.

Unlike the setup link, these must survive a restart. A key regenerated on the second
start does not fail, it makes everything written under the first one unreadable, and
that is the quieter of the two failures. So they are written once to the volume, and
a later start reads rather than generates.

Whatever the operator supplies always wins. The file also records a fingerprint of
the key that was in force, so the one genuinely dangerous transition -- an
installation that ran on an operator-supplied key and then starts without it -- is
refused instead of silently continuing under a fresh key that cannot read anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

# An external service owns these: we would be inventing a password that authenticates
# against nothing. On Compact the operator supplies both inside FS_DB_DSN and
# RABBITMQ_URL, which is why they can be absent from the environment and still work.
EXTERNALLY_OWNED_SECRETS = frozenset({"POSTGRES_PASSWORD", "RABBITMQ_PASS"})

_STATE_VERSION = 1
_FINGERPRINT_CONTEXT = b"flow-steward.compact-runtime-secret.v1"


def self_minted_secret_names() -> tuple[str, ...]:
    """The secrets this runtime must be able to produce for itself, in order."""
    from core.infrastructure.config.env_registry import installer_generated_secret_names
    from core.infrastructure.runtime.compact.process_spec import delivered_environment_names

    generated = installer_generated_secret_names()
    delivered = delivered_environment_names()
    return tuple(sorted((generated & delivered) - EXTERNALLY_OWNED_SECRETS))


@dataclass(frozen=True)
class RuntimeSecretOutcome:
    """What this start had to do, with no values in it."""

    minted: tuple[str, ...] = ()
    reused: tuple[str, ...] = ()
    operator_supplied: tuple[str, ...] = ()
    rotated: tuple[str, ...] = field(default=())


class OperatorSecretWithdrawn(RuntimeError):
    """An operator-supplied secret is gone and a replacement could not read the data."""


def establish_runtime_secrets(
    *,
    state_path: Path,
    environment: MutableMapping[str, str],
) -> RuntimeSecretOutcome:
    """Fill in every secret nobody supplied, once, and keep it across restarts."""
    state = _read_state(state_path)
    minted_values: dict[str, str] = dict(state.get("minted") or {})
    fingerprints: dict[str, str] = dict(state.get("fingerprints") or {})
    minted: list[str] = []
    reused: list[str] = []
    supplied: list[str] = []
    rotated: list[str] = []

    for name in self_minted_secret_names():
        provided = str(environment.get(name) or "").strip()
        if provided:
            supplied.append(name)
            digest = _fingerprint(provided)
            if fingerprints.get(name) not in {None, digest}:
                rotated.append(name)
            fingerprints[name] = digest
            continue
        stored = str(minted_values.get(name) or "").strip()
        if stored:
            environment[name] = stored
            reused.append(name)
            continue
        if name in fingerprints:
            raise OperatorSecretWithdrawn(
                f"{name} was supplied for this installation and is now absent. Generating a "
                "replacement would make everything already written under it unreadable, so "
                f"this runtime will not start without it. Supply {name} again, or remove its "
                f"entry from {state_path} to accept that loss deliberately."
            )
        value = secrets.token_urlsafe(48)
        minted_values[name] = value
        fingerprints[name] = _fingerprint(value)
        environment[name] = value
        minted.append(name)

    if minted or fingerprints != dict(state.get("fingerprints") or {}):
        _write_state(state_path, minted=minted_values, fingerprints=fingerprints)
    return RuntimeSecretOutcome(
        minted=tuple(minted),
        reused=tuple(reused),
        operator_supplied=tuple(supplied),
        rotated=tuple(rotated),
    )


def _fingerprint(value: str) -> str:
    return hashlib.sha256(_FINGERPRINT_CONTEXT + value.encode("utf-8")).hexdigest()


def _read_state(state_path: Path) -> dict[str, object]:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != _STATE_VERSION:
        # A file this runtime cannot read is not a file it may overwrite: doing so
        # would discard keys it cannot see and produce exactly the silent unreadable
        # data this module exists to prevent.
        if state_path.exists():
            raise OperatorSecretWithdrawn(
                f"{state_path} exists and is not a runtime secret state file this version "
                "understands. Move it aside deliberately rather than letting the runtime "
                "replace keys it cannot read."
            )
        return {}
    return payload


def _write_state(
    state_path: Path,
    *,
    minted: dict[str, str],
    fingerprints: dict[str, str],
) -> None:
    payload = {
        "version": _STATE_VERSION,
        "minted": dict(sorted(minted.items())),
        "fingerprints": dict(sorted(fingerprints.items())),
    }
    state_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    temporary = state_path.parent / f".{state_path.name}.tmp"
    # 0600 and never handed to a child: the processes receive the values through their
    # own environment, so nothing but PID 1 has a reason to read this.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, state_path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "EXTERNALLY_OWNED_SECRETS",
    "OperatorSecretWithdrawn",
    "RuntimeSecretOutcome",
    "establish_runtime_secrets",
    "self_minted_secret_names",
]
