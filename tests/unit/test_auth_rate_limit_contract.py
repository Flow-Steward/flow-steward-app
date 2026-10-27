from __future__ import annotations

import inspect
from typing import get_overloads

from core.application.auth import rate_limit


def test_clear_user_login_failures_contract_keeps_keyword_only_store() -> None:
    clear_user_login_failures = getattr(rate_limit, "ClearUserLoginFailures", None)
    assert clear_user_login_failures is not None

    overloads = get_overloads(clear_user_login_failures.__call__)
    assert len(overloads) == 2

    fallback_signature = inspect.signature(overloads[0])
    shared_store_signature = inspect.signature(overloads[1])
    assert list(fallback_signature.parameters) == ["self", "username"]
    assert list(shared_store_signature.parameters) == ["self", "username", "bl"]
    assert shared_store_signature.parameters["bl"].kind is inspect.Parameter.KEYWORD_ONLY
