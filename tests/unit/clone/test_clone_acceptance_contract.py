from tests.support.disposable_clone import (
    CLONE_ACCEPTANCE_CASES,
    DOMAIN_JOURNEYS,
    CloneAcceptanceEvidence,
    CloneAcceptanceInputs,
)


def test_acceptance_matrix_covers_profiles_journeys_and_failure_boundaries() -> None:
    cases = {case.case_id: case for case in CLONE_ACCEPTANCE_CASES}

    assert {case.profile for case in cases.values()} == {"compact", "scalable"}
    assert {journey.journey_id for journey in DOMAIN_JOURNEYS} == {
        "same-domain",
        "different-domain",
        "production-to-dev",
        "clone-domain-cutover",
    }
    assert {
        "populated-target",
        "confirmation-declined",
        "corrupt-archive",
        "wrong-passphrase",
        "exact-release-unavailable",
        "registry-fallback",
        "external-effects",
        "interruption-resume",
    }.issubset(cases)
    assert cases["confirmation-declined"].mutation_expected is False
    assert cases["corrupt-archive"].mutation_expected is False
    assert cases["wrong-passphrase"].mutation_expected is False
    assert cases["exact-release-unavailable"].mutation_expected is False


def test_live_inputs_report_stable_blockers_instead_of_fabricating_evidence() -> None:
    inputs = CloneAcceptanceInputs.from_environment({})

    assert inputs.ready is False
    assert inputs.blockers == (
        "FS_CLONE_ACCEPTANCE_COMMIT",
        "FS_CLONE_ACCEPTANCE_DRIVER",
        "FS_CLONE_ACCEPTANCE_IMAGE_DIGESTS_JSON",
        "FS_CLONE_ACCEPTANCE_OWNED_RESOURCES_JSON",
        "FS_CLONE_ACCEPTANCE_SOURCE_ROOT",
        "FS_CLONE_ACCEPTANCE_TAG",
        "FS_CLONE_ACCEPTANCE_TARGET_ROOT",
        "FS_CLONE_ACCEPTANCE_VERSION",
    )


def test_acceptance_evidence_requires_exact_identity_and_cleanup_results() -> None:
    evidence = CloneAcceptanceEvidence.model_validate(
        {
            "case_id": "populated-target",
            "candidate_sha": "a" * 40,
            "release_version": "1.2.3",
            "release_tag": "v1.2.3",
            "release_commit": "b" * 40,
            "image_digests": {"app": "sha256:" + "c" * 64},
            "source_installation_id": "source",
            "target_installation_id": "target",
            "assertions": {"database_parity": True, "maintenance_retained": True},
            "owned_resources": ("container:target-app",),
            "cleanup_results": {"container:target-app": "removed"},
            "status": "passed",
        }
    )

    assert evidence.status == "passed"
    assert set(evidence.cleanup_results) == set(evidence.owned_resources)
