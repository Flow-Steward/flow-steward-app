"""Provider-neutral bounded guard against reflected runtime credentials."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

_EMBEDDED_SECRET_MIN_LENGTH = 8
_MAX_SECRET_COUNT = 256
_MAX_SECRET_CODEPOINTS = 65_536
# The scan budgets are sized so that any output within the default 5 MiB
# extension output cap stays checkable: a 5 MiB JSON document holds at most
# ~2.6M nodes (``[0,0,...]``) and at most 5 Mi codepoints of distinct strings.
# Repeated strings (dict keys above all) are compared once, so the comparison
# budget is spent on distinct text only.
_MAX_COMPARISON_CODEPOINTS = 64 * 1024 * 1024
_MAX_TRAVERSAL_NODES = 3 * 1024 * 1024
_MAX_NESTING_DEPTH = 64
SECRET_EXPOSURE_CODE = "extension_secret_exposure_blocked"
SECRET_EXPOSURE_MESSAGE = (
    "The extension returned protected credential material. Contact the extension author."
)
SECRET_CHECK_INCOMPLETE_CODE = "extension_secret_check_incomplete"
SECRET_CHECK_INCOMPLETE_MESSAGE = (
    "The extension output exceeds the limits of the protected-credential check, so it was "
    "blocked unchecked. Request less data per call, for example a smaller page size."
)

SecretCheckLimit = Literal[
    "secret_type",
    "secret_inventory",
    "secret_count",
    "secret_codepoints",
    "traversal_nodes",
    "nesting_depth",
    "comparison_codepoints",
]


@dataclass(frozen=True, slots=True)
class SecretExposureFinding:
    """Why a value was blocked: a real secret match, or a check that could not finish.

    Both outcomes block the value (fail closed); they differ only in what the
    caller reports, so an oversized output is not reported as a credential leak.
    """

    reason: Literal["match", "check_incomplete"]
    limit: SecretCheckLimit | None = None

    @classmethod
    def match(cls) -> SecretExposureFinding:
        return cls(reason="match")

    @classmethod
    def check_incomplete(cls, limit: SecretCheckLimit) -> SecretExposureFinding:
        return cls(reason="check_incomplete", limit=limit)


def candidate_exposes_secret(candidate: str, secret: str) -> bool:
    if not secret:
        return False
    if candidate == secret or len(secret) >= _EMBEDDED_SECRET_MIN_LENGTH:
        return secret in candidate
    # Unicode ``\w`` is ``str.isalnum()`` plus underscore; subtracting underscore
    # preserves the existing boundary semantics while the regex search stays in C.
    alphanumeric = r"[^\W_]"
    return (
        re.search(
            rf"(?<!{alphanumeric}){re.escape(secret)}(?!{alphanumeric})",
            candidate,
        )
        is not None
    )


def _dict_nodes(
    value: dict[Any, Any],
    *,
    depth: int,
    string_keys_are_candidates: bool,
) -> Iterator[tuple[Any, int, bool, bool]]:
    for key, nested in value.items():
        yield key, depth, string_keys_are_candidates and isinstance(key, str), False
        yield nested, depth, True, True


def _sequence_nodes(
    value: list[Any] | tuple[Any, ...], *, depth: int
) -> Iterator[tuple[Any, int, bool, bool]]:
    for nested in value:
        yield nested, depth, True, True


def _nested_nodes(
    value: Any, *, string_keys_are_candidates: bool
) -> Iterator[tuple[Any, int, bool]]:
    pending: list[Iterator[tuple[Any, int, bool, bool]]] = [iter(((value, 0, True, True),))]
    while pending:
        try:
            node, depth, is_candidate, descend = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        yield node, depth, is_candidate
        if not descend:
            continue
        if isinstance(node, dict):
            pending.append(
                _dict_nodes(
                    node,
                    depth=depth + 1,
                    string_keys_are_candidates=string_keys_are_candidates,
                )
            )
        elif isinstance(node, (list, tuple)):
            pending.append(_sequence_nodes(node, depth=depth + 1))


def collect_secret_values(value: Any) -> tuple[frozenset[str], bool]:
    """Collect nested secret values within closed node, depth, and size budgets."""
    secrets: set[str] = set()
    codepoints = 0
    for node_count, (node, depth, is_candidate) in enumerate(
        _nested_nodes(value, string_keys_are_candidates=False), start=1
    ):
        if node_count > _MAX_TRAVERSAL_NODES or depth > _MAX_NESTING_DEPTH:
            return frozenset(), True
        if not is_candidate or not isinstance(node, str) or not node or node in secrets:
            continue
        secrets.add(node)
        codepoints += len(node)
        if len(secrets) > _MAX_SECRET_COUNT or codepoints > _MAX_SECRET_CODEPOINTS:
            return frozenset(), True
    return frozenset(secrets), False


def _check_incomplete(
    limit: SecretCheckLimit, observed: int, maximum: int
) -> SecretExposureFinding:
    logger.warning(
        "secret reflection check incomplete: limit=%s observed=%d maximum=%d",
        limit,
        observed,
        maximum,
    )
    return SecretExposureFinding.check_incomplete(limit)


def _normalized_secrets(
    secrets: Iterable[str],
) -> list[str] | SecretExposureFinding:
    normalized: list[str] = []
    codepoints = 0
    for secret in secrets:
        if not isinstance(secret, str):
            return _check_incomplete("secret_type", 1, 0)
        if not secret:
            continue
        normalized.append(secret)
        codepoints += len(secret)
        if len(normalized) > _MAX_SECRET_COUNT:
            return _check_incomplete("secret_count", len(normalized), _MAX_SECRET_COUNT)
        if codepoints > _MAX_SECRET_CODEPOINTS:
            return _check_incomplete("secret_codepoints", codepoints, _MAX_SECRET_CODEPOINTS)
    return normalized


def find_secret_exposure(value: Any, secrets: Iterable[str]) -> SecretExposureFinding | None:
    """Return why ``value`` must be blocked, or ``None`` when it is verifiably clean."""
    normalized_secrets = _normalized_secrets(secrets)
    if isinstance(normalized_secrets, SecretExposureFinding):
        return normalized_secrets
    if not normalized_secrets:
        return None
    remaining_work = _MAX_COMPARISON_CODEPOINTS
    compared: set[str] = set()
    for node_count, (node, depth, is_candidate) in enumerate(
        _nested_nodes(value, string_keys_are_candidates=True), start=1
    ):
        if node_count > _MAX_TRAVERSAL_NODES:
            return _check_incomplete("traversal_nodes", node_count, _MAX_TRAVERSAL_NODES)
        if depth > _MAX_NESTING_DEPTH:
            return _check_incomplete("nesting_depth", depth, _MAX_NESTING_DEPTH)
        if not is_candidate or not isinstance(node, str) or node in compared:
            continue
        compared.add(node)
        comparison_work = max(1, len(node)) * len(normalized_secrets)
        if comparison_work > remaining_work:
            return _check_incomplete(
                "comparison_codepoints",
                _MAX_COMPARISON_CODEPOINTS - remaining_work + comparison_work,
                _MAX_COMPARISON_CODEPOINTS,
            )
        remaining_work -= comparison_work
        if any(candidate_exposes_secret(node, secret) for secret in normalized_secrets):
            return SecretExposureFinding.match()
    return None


def value_exposes_secret(value: Any, secrets: Iterable[str]) -> bool:
    """Fail-closed boolean form of :func:`find_secret_exposure`."""
    return find_secret_exposure(value, secrets) is not None


def secret_exposure_invocation(
    finding: SecretExposureFinding | None = None,
) -> dict[str, Any]:
    """Return the canonical fail-closed invocation for a blocked output.

    Without a finding, or for a real match, the output reflected credentials.
    An incomplete check gets its own code and names the limit that stopped it.
    """
    if finding is None or finding.reason == "match":
        return {
            "status": "failed",
            "detail": SECRET_EXPOSURE_CODE,
            "response": {
                "ok": False,
                "error": SECRET_EXPOSURE_MESSAGE,
                "error_code": SECRET_EXPOSURE_CODE,
            },
            "errors": [{"code": SECRET_EXPOSURE_CODE}],
        }
    return {
        "status": "failed",
        "detail": SECRET_CHECK_INCOMPLETE_CODE,
        "response": {
            "ok": False,
            "error": SECRET_CHECK_INCOMPLETE_MESSAGE,
            "error_code": SECRET_CHECK_INCOMPLETE_CODE,
            "limit": finding.limit,
        },
        "errors": [{"code": SECRET_CHECK_INCOMPLETE_CODE, "limit": finding.limit}],
    }


__all__ = [
    "SECRET_CHECK_INCOMPLETE_CODE",
    "SECRET_CHECK_INCOMPLETE_MESSAGE",
    "SECRET_EXPOSURE_CODE",
    "SECRET_EXPOSURE_MESSAGE",
    "SecretCheckLimit",
    "SecretExposureFinding",
    "candidate_exposes_secret",
    "collect_secret_values",
    "find_secret_exposure",
    "secret_exposure_invocation",
    "value_exposes_secret",
]
