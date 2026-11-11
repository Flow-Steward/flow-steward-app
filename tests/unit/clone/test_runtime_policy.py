from __future__ import annotations

import hashlib
import re

from core.application.clone.runtime_policy import _postgres_identity_fingerprint

_IDENTITY = b'{"database":"flow_steward","password":"summer2026","user":"flow_steward"}'  # pragma: allowlist secret


def test_the_postgres_identity_fingerprint_is_not_a_fast_hash_of_the_password() -> None:
    fingerprint = _postgres_identity_fingerprint(_IDENTITY, installation_id="fs-target")

    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    assert fingerprint != hashlib.sha256(_IDENTITY).hexdigest()


def test_the_fingerprint_is_stable_for_one_target_and_salted_per_installation() -> None:
    first = _postgres_identity_fingerprint(_IDENTITY, installation_id="fs-target")

    assert _postgres_identity_fingerprint(_IDENTITY, installation_id="fs-target") == first
    assert _postgres_identity_fingerprint(_IDENTITY, installation_id="fs-other") != first
