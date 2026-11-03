"""An opaque stand-in for a configured secret value.

A secret input never travels as plaintext. What travels is this reference: it
carries the field it belongs to and the key of the encrypted record holding the
value, and nothing else. It survives mappings, aliases, loops and child-workflow
calls unchanged, so a run can pass a credential around without any step,
checkpoint, prompt, log or Test Run response ever holding the value.

Resolution happens at one place — an authorized backend tool or connector
boundary — which is the only code that calls `resolve_secret_reference`. The
value itself comes from the host-selected `StoredSecretReader`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from core.infrastructure.security.stored_secrets import (
    SecretReadContext,
    StoredSecretMissing,
    StoredSecretReader,
    StoredSecretUndecryptable,
)

#: Wire form. A checkpoint or JSON payload round-trips through this shape, so a
#: reference that crosses a serialization boundary is still a reference and not
#: a string that happens to look like one.
SECRET_REFERENCE_MARKER = "flow-steward.secret-ref.v1"
REDACTED_SECRET_REFERENCE_MARKER = "flow-steward.redacted-secret-ref.v1"


class SecretReference:
    """A pointer to an encrypted value, safe to carry anywhere in a run."""

    __slots__ = ("ciphertext", "name", "ref_key")

    def __init__(self, *, name: str, ref_key: str = "", ciphertext: str = "") -> None:
        self.name = str(name or "").strip()
        #: Points at a stored value an operator configured for this workflow.
        self.ref_key = str(ref_key or "").strip()
        #: Or carries the value itself, encrypted, for a secret a caller
        #: supplied for this run alone. Storing that in the account's secret
        #: table would mean writing a row per run and collecting it later; the
        #: point is only that the run never carries plaintext, and encrypted
        #: is encrypted wherever it sits.
        self.ciphertext = str(ciphertext or "")

    @property
    def is_inline(self) -> bool:
        return bool(self.ciphertext)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretReference):
            return NotImplemented
        return (
            self.name == other.name
            and self.ref_key == other.ref_key
            and self.ciphertext == other.ciphertext
        )

    def __hash__(self) -> int:
        return hash((self.name, self.ref_key, self.ciphertext))

    def __repr__(self) -> str:
        # Deliberately not a value: a reference printed into a log or traceback
        # must stay a reference. The ref key is an identifier, not a credential,
        # but there is no reason to print even that by default.
        return f"<SecretReference {self.name or '<unnamed>'}>"

    __str__ = __repr__

    def to_wire(self) -> dict[str, str]:
        wire = {
            "__type__": SECRET_REFERENCE_MARKER,
            "name": self.name,
            "ref_key": self.ref_key,
        }
        if self.ciphertext:
            wire["ciphertext"] = self.ciphertext
        return wire

    @classmethod
    def from_wire(cls, value: Any) -> SecretReference | None:
        if not isinstance(value, dict) or value.get("__type__") != SECRET_REFERENCE_MARKER:
            return None
        return cls(
            name=str(value.get("name") or ""),
            ref_key=str(value.get("ref_key") or ""),
            ciphertext=str(value.get("ciphertext") or ""),
        )


def is_secret_reference(value: Any) -> bool:
    """Whether a value is a secret reference in object or wire form."""
    return isinstance(value, SecretReference) or SecretReference.from_wire(value) is not None


def as_secret_reference(value: Any) -> SecretReference | None:
    if isinstance(value, SecretReference):
        return value
    return SecretReference.from_wire(value)


def inline_secret_ref_fingerprint(reference: Any) -> str:
    """Return a non-revealing launch capability for one inline reference."""
    resolved = as_secret_reference(reference)
    if resolved is None or not resolved.is_inline:
        return ""
    # The encrypted value is the capability identity. A trusted child mapping
    # may alias the parent field name without minting a new capability.
    owner = f"{len(resolved.ciphertext)}:{resolved.ciphertext}".encode()
    return hashlib.sha256(b"workflow-inline-secret-ref\0" + owner).hexdigest()


class SecretReferenceUnauthorized(RuntimeError):
    """A reference named a key the running workflow never declared.

    Run state is authored: a literal in an input mapping is enough to put a
    reference into it, and the key is derivable from a workflow and field name.
    Resolving whatever key arrives would make this boundary an oracle for every
    secret in the account, so the key is checked against what the workflow
    itself declares rather than trusted because it looks well formed.
    """

    def __init__(self, reference: SecretReference) -> None:
        super().__init__(
            f"Secret input '{reference.name or '<unnamed>'}' is not declared by this workflow."
        )
        self.reference = reference


class SecretReferenceUnresolved(RuntimeError):
    """A reference reached the boundary and no value was found for it.

    Returning the reference itself, or an empty string, would send a request
    carrying a credential that is not a credential — and the failure would
    surface downstream as someone else's authentication error, far from the
    configuration that caused it.
    """

    def __init__(self, reference: SecretReference) -> None:
        super().__init__(
            f"No stored value for secret input '{reference.name or '<unnamed>'}'. "
            "Set it again in the workflow's input settings."
        )
        self.reference = reference


def _decrypt_inline(ciphertext: str) -> str:
    from core.infrastructure.security.secret_codec import decrypt_secret

    return decrypt_secret(ciphertext)


def resolve_secret_reference(
    reference: Any,
    *,
    context: SecretReadContext,
    authorized_ref_keys: frozenset[str],
    reader: StoredSecretReader,
    authorized_inline_ref_fingerprints: frozenset[str] = frozenset(),
    decrypt_inline: Callable[[str], str] = _decrypt_inline,
) -> str:
    """Exchange one reference for its plaintext. Only an authorized boundary calls this.

    `authorized_ref_keys` is what the running workflow declares. It is required
    rather than optional: a default of "everything" is the version of this
    function that leaks, and a caller that forgets the argument should fail to
    call it at all. Authorization is decided here, before `reader` is asked.

    A stored value comes from `reader`, whichever store the host selected. An
    inline value is this run's own ciphertext and is always decrypted locally:
    it was never in any store, so no reader can produce it.
    """
    resolved = as_secret_reference(reference)
    if resolved is None:
        raise TypeError("value is not a secret reference")
    if resolved.is_inline:
        fingerprint = inline_secret_ref_fingerprint(resolved)
        if not fingerprint or fingerprint not in authorized_inline_ref_fingerprints:
            raise SecretReferenceUnauthorized(resolved)
        try:
            plaintext = decrypt_inline(resolved.ciphertext)
        except Exception:
            raise StoredSecretUndecryptable() from None
        if not plaintext:
            raise SecretReferenceUnresolved(resolved)
        return plaintext
    if not resolved.ref_key or resolved.ref_key not in authorized_ref_keys:
        raise SecretReferenceUnauthorized(resolved)
    try:
        plaintext = reader.read(context, resolved.ref_key)
    except StoredSecretMissing:
        raise SecretReferenceUnresolved(resolved) from None
    if not plaintext:
        raise SecretReferenceUnresolved(resolved)
    return plaintext


def resolve_secret_references_in(
    payload: Any,
    *,
    context: SecretReadContext,
    authorized_ref_keys: frozenset[str],
    reader: StoredSecretReader,
    authorized_inline_ref_fingerprints: frozenset[str] = frozenset(),
    decrypt_inline: Callable[[str], str] = _decrypt_inline,
) -> Any:
    """Resolve every reference inside a payload, leaving everything else alone.

    A connector's inputs are a nested structure, and a credential can sit at any
    depth in it. Walking the payload here means the boundary is one call rather
    than a rule each step has to remember.
    """

    def walk(node: Any) -> Any:
        if is_secret_reference(node):
            return resolve_secret_reference(
                node,
                context=context,
                authorized_ref_keys=authorized_ref_keys,
                reader=reader,
                authorized_inline_ref_fingerprints=authorized_inline_ref_fingerprints,
                decrypt_inline=decrypt_inline,
            )
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(payload)


__all__ = [
    "REDACTED_SECRET_REFERENCE_MARKER",
    "SECRET_REFERENCE_MARKER",
    "SecretReference",
    "SecretReferenceUnauthorized",
    "SecretReferenceUnresolved",
    "as_secret_reference",
    "inline_secret_ref_fingerprint",
    "is_secret_reference",
    "resolve_secret_reference",
    "resolve_secret_references_in",
]
