"""Download addresses for a run's final files on the job page."""

from __future__ import annotations

from urllib.parse import quote


def with_artifact_downloads(result: dict, job_id: str) -> dict:
    """Give each final file of the run its download address on this job.

    ``build_public_result`` keeps only artifacts that belong to the job, and the
    job's artifact route authorizes the same ownership, so the address is real.
    """
    def _linked(item: object) -> object:
        if not isinstance(item, dict) or not item.get("artifact_id"):
            return item
        url = f"/api/jobs/{quote(str(job_id), safe='')}/artifacts/{quote(str(item['artifact_id']), safe='')}"
        return {**item, "download_url": url}

    artifacts = result.get("artifacts")
    if isinstance(artifacts, dict):
        result["artifacts"] = {
            name: [_linked(entry) for entry in value] if isinstance(value, list) else _linked(value)
            for name, value in artifacts.items()
        }
    return result
