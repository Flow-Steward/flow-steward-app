import pytest

from tests.support.disposable_clone import (
    CLONE_ACCEPTANCE_CASES,
    CloneAcceptanceInputs,
    run_live_case,
)


@pytest.mark.parametrize("case", CLONE_ACCEPTANCE_CASES, ids=lambda case: case.case_id)
def test_opt_in_live_clone_matrix_records_real_evidence_only(case) -> None:
    inputs = CloneAcceptanceInputs.from_environment()
    if not inputs.ready:
        pytest.skip("Clone live acceptance blocked: " + ", ".join(inputs.blockers))

    evidence = run_live_case(inputs, case)

    assert evidence.case_id == case.case_id
    assert evidence.status == case.expected_status
    assert all(evidence.assertions.values())
    assert set(evidence.cleanup_results) == set(evidence.owned_resources)
