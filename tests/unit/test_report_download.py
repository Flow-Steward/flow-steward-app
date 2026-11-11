from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.entrypoints.api.report_download import report_download_response

_REQUEST = SimpleNamespace(headers={}, url=SimpleNamespace(scheme="https"))


@pytest.mark.parametrize(
    "token",
    ["", "short", "a" * 65, "abcdefghijklmnop; Path=/", "abcdefghijklmnop\r\nSet-Cookie: x=1"],
)
def test_report_download_refuses_a_token_that_cannot_name_the_cookie(token: str) -> None:
    with pytest.raises(ValueError, match="REPORT_DOWNLOAD_TOKEN_PATTERN"):
        report_download_response(
            _REQUEST, filename="report.json", download_token=token, render=lambda: b"{}"
        )


def test_report_download_names_the_cookie_after_a_valid_token() -> None:
    response = report_download_response(
        _REQUEST,
        filename="report.json",
        download_token="Abc_def-1234567890",
        render=lambda: b"{}",
    )

    cookie = response.headers["set-cookie"]
    assert cookie.startswith("fs_job_report_download_Abc_def-1234567890=1;")
    assert "Secure" in cookie
