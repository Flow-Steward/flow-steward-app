"""AC19: drive the benchmark workflow through a real installation and measure it.

Standard library only: it runs on the Docker host from the checkout, against the
installation's public API, and reads container figures from the Docker CLI.

Each job goes the product's whole way -- API, queue, a worker, checkpoints in
PostgreSQL -- and is timed from submission to its terminal status as the API reports
it. Two loads:

- closed loop: `concurrency` submissions in flight until `jobs` have finished, for
  throughput and latency at a given worker count;
- open loop: jobs submitted at each rate of a sweep, to find where /api/workload
  changes its recommendation from `normal`.

Every second it records /api/workload and `docker stats` for the compose project's
containers.
"""

from __future__ import annotations

import json
import os
import ssl
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# AC17-R155: the full job detail is the job page's contract -- diagnostics, redacted
# previews, the whole result -- and polling it for every job in flight made this tool
# load the API it was measuring. The compact form carries the status and costs a
# fraction; what polling costs is reported with every run.
STATUS_PATH = "/api/v2/jobs/{job_id}?compact=true&step_limit=0"
POLL_INTERVAL_SECONDS = 0.5

TERMINAL_STATUSES = frozenset(
    {"cancelled", "completed", "failed", "max_iterations", "triage_needed"}
)


@dataclass(frozen=True)
class Target:
    base_url: str
    token: str
    project_id: str
    workflow_id: str
    ca_file: str = ""
    compose_project: str = ""


@dataclass
class JobRecord:
    job_id: str
    submitted_at: float
    finished_at: float | None = None
    status: str = ""
    error: str = ""
    # Submissions the installation turned away with 429 before this one was accepted:
    # its own backpressure, measured rather than counted as failures.
    admission_rejections: int = 0
    # Status requests made while waiting, and the time the installation spent on them.
    polls: int = 0
    poll_seconds: float = 0.0


@dataclass
class Samples:
    workload: list[dict[str, Any]] = field(default_factory=list)
    containers: list[dict[str, Any]] = field(default_factory=list)


class ApiError(RuntimeError):
    """An answer from the installation that was not a success, with what it said."""

    def __init__(self, message: str, *, status: int = 0, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class Api:
    def __init__(self, target: Target) -> None:
        self._target = target
        # An operator's own stand may serve a self-signed certificate: trust that
        # certificate on request, and still verify the chain and the host name.
        self._context = (
            ssl.create_default_context(cafile=target.ca_file) if target.ca_file else None
        )

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(  # nosec B310 - the operator's own installation
            self._target.base_url.rstrip("/") + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._target.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(  # nosec B310 - the operator's own installation
                request, timeout=30, context=self._context
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            try:
                retry_after = float(exc.headers.get("Retry-After") or 0)
            except ValueError:
                retry_after = 0.0
            raise ApiError(
                f"{method} {path} -> HTTP {exc.code}: {detail}",
                status=exc.code,
                retry_after=retry_after,
            ) from exc
        return json.loads(raw.decode("utf-8")) if raw else {}


BENCHMARK_PROJECT_NAME = "AC19 benchmark"  # a prefix: each project gets a timestamp


def setup_project(
    api: Api, *, workflow_id: str, concurrency_cap: int, workspace_id: str = ""
) -> dict[str, Any]:
    """Create a dedicated project, link the benchmark workflow, and lift its job cap.

    A project runs one job at a time unless its policy says otherwise, which would cap
    every measurement at one job whatever the worker count -- so the cap is set, and
    recorded, rather than left to its default.
    """
    if not workspace_id:
        workspace_id = _visible_workspace(api)
    name = f"{BENCHMARK_PROJECT_NAME} {time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
    created = api.request(
        "POST",
        "/api/projects",
        {"name": name, "workspace_id": workspace_id},
    )
    project_id = str((created or {}).get("project_id") or (created or {}).get("id") or "")
    if not project_id:
        raise RuntimeError(f"project creation returned no id: {created}")
    api.request(
        "PUT",
        f"/api/projects/{project_id}/workflows",
        {"workflow_ids": [workflow_id], "operator_default_workflow_id": workflow_id},
    )
    api.request("PUT", f"/api/projects/{project_id}/policy", {"concurrency_cap": concurrency_cap})
    policy = api.request("GET", f"/api/projects/{project_id}/policy")
    return {"project_id": project_id, "workflow_id": workflow_id, "policy": policy}


def _visible_workspace(api: Api) -> str:
    """A workspace to create the project in, found with the scopes a UI token can hold.

    T1: /api/workspaces needs admin:all, which no token created in the UI carries; a
    project the token can see (projects.read) names its workspace. /api/workspaces stays
    the answer for an installation with no project yet.
    """
    listed = api.request("GET", "/api/projects?per_page=1")
    projects = (listed or {}).get("projects") or []
    if projects and str(projects[0].get("workspace_id") or "").strip():
        return str(projects[0]["workspace_id"])
    try:
        workspaces = (api.request("GET", "/api/workspaces") or {}).get("workspaces") or []
    except ApiError as exc:
        raise RuntimeError(
            "no project is visible to name a workspace, and listing workspaces needs "
            "admin:all; pass --workspace-id"
        ) from exc
    if not workspaces:
        raise RuntimeError("the installation lists no workspace to create the project in")
    return str(workspaces[0]["id"])


def cleanup_project(api: Api, *, project_id: str) -> dict[str, Any]:
    """Remove the benchmark project, only if it is the one this tool creates."""
    project = api.request("GET", f"/api/projects/{project_id}")
    name = str((project or {}).get("name") or ((project or {}).get("project") or {}).get("name"))
    if not name.startswith(BENCHMARK_PROJECT_NAME):
        raise RuntimeError(f"project {project_id} is not the benchmark project ({name!r})")
    return api.request("DELETE", f"/api/projects/{project_id}") or {"deleted": project_id}


def benchmark_rows(count: int) -> list[dict[str, Any]]:
    return [
        {"sku": f"  SKU-{index:06d}  ", "title": f"  Item {index}  ", "quantity": index % 97}
        for index in range(count)
    ]


def submit_admitted(
    api: Api, target: Target, rows: list[dict[str, Any]], *, deadline: float
) -> tuple[str, int]:
    """Submit, honouring the installation's 429 backpressure until the budget ends."""
    rejections = 0
    while True:
        try:
            return submit(api, target, rows), rejections
        except ApiError as exc:
            if exc.status != 429 or time.monotonic() >= deadline:
                raise
            rejections += 1
            time.sleep(max(0.2, exc.retry_after or 1.0))


def submit(api: Api, target: Target, rows: list[dict[str, Any]]) -> str:
    body = api.request(
        "POST",
        "/api/workflow-runs",
        {
            "project_id": target.project_id,
            "workflow_id": target.workflow_id,
            "inputs": {"payload": {"rows": rows}},
        },
    )
    job_id = str((body or {}).get("job_id") or (body or {}).get("id") or "").strip()
    if not job_id:
        raise RuntimeError(f"/api/workflow-runs returned no job id: {body}")
    return job_id


def wait_terminal(
    api: Api, record: JobRecord, *, timeout: float, clock: Callable[[], float]
) -> None:
    deadline = clock() + timeout
    while clock() < deadline:
        asked_at = clock()
        try:
            envelope = api.request("GET", STATUS_PATH.format(job_id=record.job_id))
        except (OSError, ValueError) as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        finally:
            record.polls += 1
            record.poll_seconds += max(0.0, clock() - asked_at)
        job = (envelope or {}).get("data") or {}
        status = str(job.get("status") or "").strip().lower()
        record.status = status
        if status in TERMINAL_STATUSES:
            record.finished_at = clock()
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    record.error = record.error or "timed out waiting for a terminal status"


def docker_stats(compose_project: str) -> list[dict[str, Any]]:
    if not compose_project:
        return []
    names = subprocess.run(  # nosec B603 B607 - fixed docker CLI arguments
        (
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={compose_project}",
            "--format",
            "{{.Names}}",
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    ).stdout.split()
    if not names:
        return []
    stats = subprocess.run(  # nosec B603 B607 - fixed docker CLI arguments
        ("docker", "stats", "--no-stream", "--format", "{{json .}}", *names),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    rows: list[dict[str, Any]] = []
    for line in stats.stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        rows.append(
            {
                "name": item.get("Name"),
                "cpu_percent": _percent(item.get("CPUPerc")),
                "memory_bytes": _bytes(str(item.get("MemUsage") or "").split("/")[0]),
            }
        )
    return rows


def _percent(value: object) -> float:
    try:
        return float(str(value or "0").strip().rstrip("%"))
    except ValueError:
        return 0.0


_UNITS = {
    "b": 1,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
}


def _bytes(value: str) -> int:
    text = value.strip().lower()
    for unit in sorted(_UNITS, key=len, reverse=True):
        if text.endswith(unit):
            try:
                return int(float(text[: -len(unit)].strip()) * _UNITS[unit])
            except ValueError:
                return 0
    return 0


@dataclass(frozen=True)
class HostLimits:
    """Stop before the benchmark hurts what else the host runs.

    The auditor's servers also serve the owner's production sites, so a run stops when
    the host is already busy, not only when the benchmark is done.
    """

    max_load_average: float = 8.0
    min_available_bytes: int = 2 * 1024**3


def available_memory_bytes() -> int | None:
    """MemAvailable from /proc/meminfo; None where the host does not expose it."""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def total_memory_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def host_pressure(limits: HostLimits) -> str:
    """The reason to stop now, or "" while the host has room."""
    load = os.getloadavg()[0]
    if load > limits.max_load_average:
        return f"host load average {load:.2f} is above {limits.max_load_average}"
    available = available_memory_bytes()
    if available is not None and available < limits.min_available_bytes:
        return f"host available memory {available} bytes is below {limits.min_available_bytes}"
    return ""


def host_facts() -> dict[str, Any]:
    return {
        "nproc": os.cpu_count(),
        "memory_total_bytes": total_memory_bytes(),
        "memory_available_bytes": available_memory_bytes(),
        "load_average": list(os.getloadavg()),
    }


def installation_facts(api: Api, target: Target) -> dict[str, Any]:
    """The revision the installation serves and the worker count actually running."""
    facts: dict[str, Any] = {}
    try:
        live = api.request("GET", "/health/live")
        facts["build_sha"] = (live or {}).get("build_sha")
        facts["build_version"] = (live or {}).get("build_version")
    except (OSError, ValueError) as exc:
        facts["build_error"] = type(exc).__name__
    services = _compose_services(target.compose_project)
    facts["profile"] = "compact" if "compact-runtime" in services else "scalable"
    if facts["profile"] == "scalable":
        facts["workers_running"] = services.count("fs-worker")
    else:
        try:
            processes = api.request("GET", "/api/processes")
            facts["workers_running"] = len((processes or {}).get("worker_processes") or [])
        except (OSError, ValueError) as exc:
            facts["workers_running_error"] = type(exc).__name__
    return facts


def _compose_services(compose_project: str) -> list[str]:
    if not compose_project:
        return []
    listed = subprocess.run(  # nosec B603 B607 - fixed docker CLI arguments
        (
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={compose_project}",
            "--format",
            '{{.Label "com.docker.compose.service"}}',
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    return listed.stdout.split()


class Sampler:
    """Records /api/workload and container figures once a second until stopped."""

    def __init__(
        self, api: Api, target: Target, samples: Samples, *, limits: HostLimits | None = None
    ) -> None:
        self._api = api
        self._target = target
        self._samples = samples
        self._stop = threading.Event()
        self.left_normal = threading.Event()
        self.pressure = ""
        self.limits = limits or HostLimits()
        self._thread = threading.Thread(target=self._loop, name="benchmark-sampler", daemon=True)

    def __enter__(self) -> Sampler:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=35)

    def _loop(self) -> None:
        while not self._stop.is_set():
            moment = time.time()
            reason = host_pressure(self.limits)
            if reason and not self.pressure:
                self.pressure = reason
            try:
                signal = self._api.request("GET", "/api/workload")
                self._samples.workload.append({"t": moment, **(signal or {})})
                if str((signal or {}).get("recommendation") or "normal") != "normal":
                    self.left_normal.set()
            except (OSError, ValueError) as exc:
                self._samples.workload.append({"t": moment, "error": type(exc).__name__})
            for row in docker_stats(self._target.compose_project):
                self._samples.containers.append({"t": moment, **row})
            self._stop.wait(1.0)


def closed_loop(
    api: Api,
    target: Target,
    *,
    jobs: int,
    concurrency: int,
    rows: int,
    timeout: float,
    deadline: float,
    should_stop: Callable[[], str] = lambda: "",
) -> tuple[list[JobRecord], str]:
    """Keep `concurrency` jobs in flight until `jobs` finish, the budget ends, or a stop.

    Returns the records and why submission stopped early ("" when it did not).
    """
    payload = benchmark_rows(rows)
    records: list[JobRecord] = []
    lock = threading.Lock()
    remaining = [jobs]
    stopped = [""]

    def worker() -> None:
        while True:
            with lock:
                reason = should_stop() or (
                    "time budget reached" if time.monotonic() >= deadline else ""
                )
                if reason and not stopped[0]:
                    stopped[0] = reason
                if remaining[0] <= 0 or stopped[0]:
                    return
                remaining[0] -= 1
            started = time.monotonic()
            try:
                job_id, rejected = submit_admitted(api, target, payload, deadline=deadline)
                record = JobRecord(
                    job_id=job_id, submitted_at=started, admission_rejections=rejected
                )
            except (OSError, ValueError, RuntimeError) as exc:
                record = JobRecord(job_id="", submitted_at=started, error=str(exc))
            else:
                budget_left = max(0.0, deadline - time.monotonic())
                wait_terminal(api, record, timeout=min(timeout, budget_left), clock=time.monotonic)
            with lock:
                records.append(record)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return records, stopped[0]


def open_loop(
    api: Api,
    target: Target,
    *,
    rate: float,
    seconds: float,
    rows: int,
    timeout: float,
    deadline: float,
    should_stop: Callable[[], str] = lambda: "",
) -> tuple[list[JobRecord], str]:
    """Offer jobs at `rate` for `seconds`; stop at the first stop reason or the budget.

    Each submission runs on its own thread, so a slow answer from the installation does
    not lower the arrival rate being offered -- a first live run did exactly that, 14
    submissions in 25 s at a nominal 1 per second.
    """
    payload = benchmark_rows(rows)
    records: list[JobRecord] = []
    lock = threading.Lock()
    threads: list[threading.Thread] = []
    interval = 1.0 / rate
    start = time.monotonic()
    index = 0
    stopped = ""

    def one() -> None:
        submitted = time.monotonic()
        try:
            record = JobRecord(job_id=submit(api, target, payload), submitted_at=submitted)
        except ApiError as exc:
            # An open loop keeps its arrival rate, so a submission the installation turned
            # away is recorded as such and not retried.
            record = JobRecord(
                job_id="",
                submitted_at=submitted,
                error=str(exc),
                admission_rejections=1 if exc.status == 429 else 0,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            record = JobRecord(job_id="", submitted_at=submitted, error=str(exc))
        else:
            wait_terminal(
                api,
                record,
                timeout=min(timeout, max(0.0, deadline - time.monotonic())),
                clock=time.monotonic,
            )
        with lock:
            records.append(record)

    while time.monotonic() - start < seconds:
        stopped = should_stop() or ("time budget reached" if time.monotonic() >= deadline else "")
        if stopped:
            break
        due = start + index * interval
        time.sleep(max(0.0, due - time.monotonic()))
        thread = threading.Thread(target=one, daemon=True)
        thread.start()
        threads.append(thread)
        index += 1
    for thread in threads:
        thread.join()
    return records, stopped


def summarize(records: Sequence[JobRecord], *, elapsed: float) -> dict[str, Any]:
    finished = [r for r in records if r.finished_at is not None]
    completed = [r for r in finished if r.status == "completed"]
    latencies = sorted((r.finished_at or 0.0) - r.submitted_at for r in completed)
    accepted = [r for r in records if r.job_id]
    offered_seconds = (
        max(r.submitted_at for r in records) - min(r.submitted_at for r in records)
        if len(records) > 1
        else 0.0
    )
    return {
        "submitted": len(records),
        "offered_jobs_per_s": (
            round((len(records) - 1) / offered_seconds, 3) if offered_seconds > 0 else None
        ),
        "accepted": len(accepted),
        "admission_rejections": sum(r.admission_rejections for r in records),
        "completed": len(completed),
        "other_terminal": len(finished) - len(completed),
        "unfinished": len(accepted) - len(finished),
        "errors": sorted({r.error for r in records if r.error})[:10],
        "throughput_jobs_per_s": round(len(completed) / elapsed, 3) if elapsed > 0 else 0.0,
        "latency_p50_s": round(statistics.median(latencies), 3) if latencies else None,
        "latency_p95_s": (
            round(latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))], 3)
            if latencies
            else None
        ),
        "polling": {
            "endpoint": STATUS_PATH,
            "interval_s": POLL_INTERVAL_SECONDS,
            "requests": sum(r.polls for r in records),
            "api_seconds": round(sum(r.poll_seconds for r in records), 3),
            "requests_per_s": (
                round(sum(r.polls for r in records) / elapsed, 3) if elapsed > 0 else None
            ),
        },
    }


def workload_transitions(samples: Samples) -> list[dict[str, Any]]:
    """Every change of recommendation, with the metrics that caused it."""
    changes: list[dict[str, Any]] = []
    previous = None
    for sample in samples.workload:
        current = sample.get("recommendation")
        if current and current != previous:
            changes.append(
                {
                    "t": sample["t"],
                    "recommendation": current,
                    "metrics": sample.get("metrics"),
                }
            )
            previous = current
    return changes


def container_peaks(samples: Samples) -> dict[str, dict[str, float]]:
    peaks: dict[str, dict[str, float]] = {}
    for row in samples.containers:
        name = str(row.get("name") or "")
        peak = peaks.setdefault(name, {"memory_bytes": 0.0, "cpu_percent": 0.0})
        peak["memory_bytes"] = max(peak["memory_bytes"], float(row.get("memory_bytes") or 0))
        peak["cpu_percent"] = max(peak["cpu_percent"], float(row.get("cpu_percent") or 0))
    return peaks


__all__ = [
    "BENCHMARK_PROJECT_NAME",
    "Api",
    "HostLimits",
    "Sampler",
    "Samples",
    "Target",
    "cleanup_project",
    "closed_loop",
    "container_peaks",
    "host_facts",
    "host_pressure",
    "installation_facts",
    "open_loop",
    "setup_project",
    "summarize",
    "workload_transitions",
]
