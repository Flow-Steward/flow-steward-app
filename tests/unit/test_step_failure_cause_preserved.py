"""A failed step must reach the operator with the cause the runtime produced.

Regression for the workflow E2E ledger findings F12/F15: a `tabular_feed` failure
arrived as `tabular_feed step failed` with error code `tabular_feed_failed` — in
the UI, in `jobs.last_error` and in the `unresolved_step_failure` channel — while
the engine had returned a structured `errors: [{code: "too_many_rows", ...}]` that
was discarded. The same envelope already renders `dataset_query` errors precisely;
the other structured step kinds were simply not routed through it.

The safe-message rules are part of the contract: an upstream message that could
carry a DSN, a credential or a request body must not be echoed verbatim.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.orchestration.active_step import ActiveStep
from core.application.orchestration.step_failures import (
    unresolved_step_failure_payload as _unresolved_step_failure_payload,
)


def _failed_step(step_id: str = "read_feed") -> ActiveStep:
    """The failed step execution the report is about."""
    return ActiveStep(
        step={"step_id": step_id, "step_kind": "tabular_feed"},
        step_id=step_id,
        step_execution_id="00000000-0000-0000-0000-000000000001",
        attempt=1,
    )


def _message(*, kind: str, errors: list[dict], summary: str, error: str) -> dict:
    return {
        "status": "fail",
        "summary": summary,
        "correlation_id": "corr-1",
        "errors": [],
        "artifacts": {
            "step_id": "read_feed",
            "step_kind": kind,
            "error": error,
            kind: {"ok": False, "errors": errors, "detail": summary},
        },
    }


# ---------------------------------------------------------------------------
# tabular_feed
# ---------------------------------------------------------------------------


def test_tabular_feed_error_code_reaches_the_operator():
    """`too_many_rows` must survive instead of collapsing to `tabular_feed_failed`."""
    message = _message(
        kind="tabular_feed",
        errors=[
            {
                "code": "too_many_rows",
                "message": "Dataset has 134933 rows, above the 100000 scan maximum",
                "path": "",
            }
        ],
        summary="tabular_feed step failed",
        error="tabular_feed_failed",
    )

    payload = _unresolved_step_failure_payload(_failed_step(), status="fail", message=message)

    assert payload["error_code"] == "too_many_rows"
    assert payload["message"] != "tabular_feed step failed"
    assert "134933" in payload["message"] or "scan maximum" in payload["message"]


def test_tabular_feed_state_reference_message_is_unchanged():
    """The existing precise message for a missing state reference must not regress."""
    message = _message(
        kind="tabular_feed",
        errors=[{"reason": "state_reference_missing", "selector": "state.feed_rows"}],
        summary="tabular_feed state reference is missing",
        error="tabular_feed_failed",
    )

    payload = _unresolved_step_failure_payload(_failed_step(), status="fail", message=message)

    assert payload["error_code"] == "state_reference_missing"
    assert payload["message"] == (
        "Tabular input at state.feed_rows references missing workflow state."
    )
    assert payload["selector"] == "state.feed_rows"


# ---------------------------------------------------------------------------
# transform
# ---------------------------------------------------------------------------


def test_transform_error_code_reaches_the_operator():
    """A coded transform failure must name its code, not just "transform failed"."""
    message = _message(
        kind="transform",
        errors=[{"code": "rule_action_unsupported", "message": "action 'ceil' is not supported"}],
        summary="transform step failed",
        error="transform_failed",
    )

    payload = _unresolved_step_failure_payload(_failed_step(), status="fail", message=message)

    assert payload["error_code"] == "rule_action_unsupported"
    assert "ceil" in payload["message"]


# ---------------------------------------------------------------------------
# Nothing sensitive may be echoed back
# ---------------------------------------------------------------------------


# The fixtures below stand in for upstream text that *could* carry a secret. The
# rule keys on the marker ("://", "token", "password", "api_key"), never on the
# value, so the placeholders carry no credential-shaped strings of their own.
_MARKER = "PLACEHOLDER-NOT-A-SECRET"


@pytest.mark.parametrize(
    "unsafe_message",
    [
        f"connect failed opening postgresql://db-host:5432/app {_MARKER}",
        f"upstream rejected the request token {_MARKER}",
        f"auth failed for the mailbox password {_MARKER}",
        f"request body was api_key={_MARKER}",
    ],
)
def test_unsafe_upstream_message_is_not_echoed(unsafe_message):
    """A safe code may surface; a message that may carry a secret may not."""
    message = _message(
        kind="tabular_feed",
        errors=[{"code": "runtime_failed", "message": unsafe_message}],
        summary="tabular_feed step failed",
        error="tabular_feed_failed",
    )

    payload = _unresolved_step_failure_payload(_failed_step(), status="fail", message=message)

    assert payload["error_code"] == "runtime_failed"
    assert _MARKER not in payload["message"]
    assert payload["message"] != unsafe_message


def test_overlong_upstream_message_is_not_echoed():
    """An unbounded upstream blob must not become the operator-facing message."""
    message = _message(
        kind="tabular_feed",
        errors=[{"code": "runtime_failed", "message": "x" * 5000}],
        summary="tabular_feed step failed",
        error="tabular_feed_failed",
    )

    payload = _unresolved_step_failure_payload(_failed_step(), status="fail", message=message)

    assert len(payload["message"]) <= 512


# ---------------------------------------------------------------------------
# The structured rows must also reach the message's own `errors` list
# ---------------------------------------------------------------------------


def test_step_error_rows_keep_the_code_and_drop_unsafe_fields():
    """`errors` on the step message carries the cause without the raw payload."""
    from core.application.orchestration.step_failures import (
        safe_step_error_rows as _safe_step_error_rows,
    )

    rows = _safe_step_error_rows(
        [
            {
                "code": "too_many_rows",
                "message": "Dataset has 134933 rows, above the 100000 scan maximum",
                "path": "rows",
                "request_body": {"api_key": _MARKER},
                "connection": f"postgresql://db-host:5432/app {_MARKER}",
            }
        ]
    )

    assert rows == [
        {
            "code": "too_many_rows",
            "message": "Dataset has 134933 rows, above the 100000 scan maximum",
            "path": "rows",
        }
    ]


def test_step_error_rows_tolerate_plain_strings_and_empty_input():
    from core.application.orchestration.step_failures import (
        safe_step_error_rows as _safe_step_error_rows,
    )

    assert _safe_step_error_rows(None) == []
    assert _safe_step_error_rows([]) == []
    assert _safe_step_error_rows(["boom"]) == [{"message": "boom"}]


def test_step_error_rows_drop_an_unsafe_message_but_keep_the_code():
    from core.application.orchestration.step_failures import (
        safe_step_error_rows as _safe_step_error_rows,
    )

    rows = _safe_step_error_rows(
        [{"code": "runtime_failed", "message": f"auth failed for the password {_MARKER}"}]
    )

    assert rows == [{"code": "runtime_failed"}]


def test_step_error_texts_render_the_safe_rows_for_the_agent_message():
    """The AgentMessage carries strings, so the safe rows are rendered as text."""
    from core.application.orchestration.step_failures import (
        safe_step_error_texts as _safe_step_error_texts,
    )

    assert _safe_step_error_texts(
        [{"code": "too_many_rows", "message": "134933 rows above the 100000 maximum"}]
    ) == ["too_many_rows: 134933 rows above the 100000 maximum"]
    assert _safe_step_error_texts(
        [{"code": "runtime_failed", "message": f"auth failed for the password {_MARKER}"}]
    ) == ["runtime_failed"]
    assert _safe_step_error_texts(
        [{"reason": "state_reference_missing", "selector": "state.feed_rows"}]
    ) == ["state_reference_missing (at state.feed_rows)"]
    assert _safe_step_error_texts(None) == []
