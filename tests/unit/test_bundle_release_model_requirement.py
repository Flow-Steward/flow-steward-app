from scripts.smoke.bundle_release_checks import BundleSuite, suites_require_model_route


def _suite(*inputs: str) -> BundleSuite:
    return BundleSuite(
        name="fixture",
        path=".",
        checks=[],
        scopes=[],
        inputs=list(inputs),
        extensions=[],
        services=[],
        connections=[],
        workflows=[],
    )


def test_connector_only_bundle_does_not_require_a_model_route() -> None:
    assert suites_require_model_route([_suite("FS_SMOKE_SMTP_HOST")]) is False


def test_model_backed_bundle_requires_a_complete_model_route() -> None:
    assert suites_require_model_route([_suite("FS_SMOKE_MODEL_PROVIDER", "FS_SMOKE_MODEL")]) is True
