from __future__ import annotations


def test_safe_operator_exception_never_projects_exception_detail() -> None:
    from core.application.management.errors import safe_operator_exception

    secrets = (
        "Bearer bearer-secret",
        "AWS_SECRET_ACCESS_KEY=cloud-secret",
        "password=db-secret",
        "private-bucket/customer/invoice.pdf",
        "/srv/private/customer/invoice.pdf",
    )
    detail = " ".join(secrets)

    projected = safe_operator_exception(RuntimeError(detail))

    assert projected.startswith("RuntimeError[")
    assert "detail_sha256=" in projected
    for secret in secrets:
        assert secret not in projected
