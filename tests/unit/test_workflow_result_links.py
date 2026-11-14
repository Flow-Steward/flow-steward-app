"""The job page gets a download address for each of the run's final files."""

from __future__ import annotations

from core.application.jobs.workflow_result_links import with_artifact_downloads


def test_each_final_file_gets_its_job_download_address():
    result = {
        "outputs": {"records": 2},
        "artifacts": {
            "result_file": {"artifact_id": "art_1", "filename": "result.jsonl"},
            "pages": [{"artifact_id": "art 2"}, {"artifact_id": "art_3"}],
        },
    }
    linked = with_artifact_downloads(result, "job/1")
    assert linked["artifacts"]["result_file"]["download_url"] == "/api/jobs/job%2F1/artifacts/art_1"
    assert [item["download_url"] for item in linked["artifacts"]["pages"]] == [
        "/api/jobs/job%2F1/artifacts/art%202",
        "/api/jobs/job%2F1/artifacts/art_3",
    ]
    assert linked["outputs"] == {"records": 2}


def test_an_entry_without_an_artifact_id_is_left_as_it_is():
    result = {"artifacts": {"note": {"filename": "x"}, "odd": "text"}}
    assert with_artifact_downloads(result, "job-1")["artifacts"] == {"note": {"filename": "x"}, "odd": "text"}
