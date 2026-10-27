from core.application.reports.display_text import build_display_summary


def test_build_display_summary_prefers_first_meaningful_line():
    raw = "Task Brief: Fix login regression\n\n### Objective\nInvestigate auth flow"
    assert build_display_summary(raw) == "Fix login regression"


def test_build_display_summary_collapses_whitespace_and_truncates():
    raw = "A" * 200
    result = build_display_summary(raw, max_len=32)
    assert len(result) <= 32
    assert result.endswith("…")


def test_build_display_summary_uses_short_sentence_when_available():
    raw = "Fix webhook retries for idempotency. Keep deployment config unchanged. Extra details follow."
    assert build_display_summary(raw) == "Fix webhook retries for idempotency"


def test_build_display_summary_hides_internal_install_scoped_invoke_tag():
    raw = (
        "[invoke_workflow:extension.5cc90460-491e-45e3-a47c-c1110afc37db."
        "sample_failure_guardrail] Sample Failure Guardrail"
    )

    assert build_display_summary(raw) == "Sample Failure Guardrail"
