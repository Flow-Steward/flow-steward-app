"""AC19: the benchmark runner's own arithmetic, against a fake installation."""

from __future__ import annotations

import itertools
import threading

import pytest

from core.application.tooling.ops import profile_benchmark_run as run


class _FakeApi:
    """Completes each job after a fixed number of polls; answers /api/workload."""

    def __init__(self, polls_to_finish: int = 2, status: str = "completed") -> None:
        self.polls_to_finish = polls_to_finish
        self.status = status
        self.submitted: list[dict] = []
        self.polls: dict[str, int] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def request(self, method, path, body=None):
        with self._lock:
            if method == "POST" and path == "/api/workflow-runs":
                self.submitted.append(body)
                return {"job_id": f"job-{next(self._ids)}"}
            if path.startswith("/api/v2/jobs/") and path.endswith("?compact=true&step_limit=0"):
                job = path.split("?", 1)[0].rsplit("/", 1)[1]
                self.polls[job] = self.polls.get(job, 0) + 1
                done = self.polls[job] >= self.polls_to_finish
                return {"data": {"status": self.status if done else "in_progress"}}
            if path == "/api/workload":
                return {"recommendation": "normal", "metrics": {"active_jobs": 0}}
        raise AssertionError(f"unexpected {method} {path}")


_TARGET = run.Target(base_url="https://x", token="t", project_id="p", workflow_id="w")


def test_closed_loop_submits_every_job_with_the_rows_as_workflow_input(monkeypatch) -> None:
    monkeypatch.setattr(run.time, "sleep", lambda _s: None)
    api = _FakeApi()

    records, stopped = run.closed_loop(
        api, _TARGET, jobs=7, concurrency=3, rows=4, timeout=5, deadline=float("inf")
    )

    assert stopped == ""
    assert len(records) == 7
    assert all(record.status == "completed" for record in records)
    first = api.submitted[0]
    assert (first["project_id"], first["workflow_id"]) == ("p", "w")
    assert set(first) == {"project_id", "workflow_id", "inputs"}
    assert len(first["inputs"]["payload"]["rows"]) == 4
    assert first["inputs"]["payload"]["rows"][0]["sku"].strip() == "SKU-000000"


def test_a_job_that_never_finishes_is_reported_not_counted(monkeypatch) -> None:
    monkeypatch.setattr(run.time, "sleep", lambda _s: None)
    api = _FakeApi(polls_to_finish=10**9)
    record = run.JobRecord(job_id="job-1", submitted_at=0.0)
    ticks = iter(range(100))

    run.wait_terminal(api, record, timeout=5, clock=lambda: float(next(ticks)))

    assert record.finished_at is None
    assert "timed out" in record.error
    summary = run.summarize([record], elapsed=5.0)
    assert (summary["completed"], summary["unfinished"]) == (0, 1)


def test_summary_counts_only_completed_jobs_toward_throughput_and_latency() -> None:
    records = [
        run.JobRecord("a", submitted_at=0.0, finished_at=1.0, status="completed"),
        run.JobRecord("b", submitted_at=0.0, finished_at=3.0, status="completed"),
        run.JobRecord("c", submitted_at=0.0, finished_at=2.0, status="failed"),
    ]

    summary = run.summarize(records, elapsed=4.0)

    assert summary["completed"] == 2
    assert summary["other_terminal"] == 1
    assert summary["throughput_jobs_per_s"] == 0.5
    assert summary["latency_p50_s"] == 2.0


def test_recommendation_changes_are_recorded_with_their_metrics() -> None:
    samples = run.Samples(
        workload=[
            {"t": 1, "recommendation": "normal", "metrics": {"active_jobs": 1}},
            {"t": 2, "recommendation": "normal", "metrics": {"active_jobs": 3}},
            {"t": 3, "recommendation": "throttle", "metrics": {"active_jobs": 5}},
            {"t": 4, "error": "URLError"},
            {"t": 5, "recommendation": "normal", "metrics": {"active_jobs": 2}},
        ]
    )

    changes = run.workload_transitions(samples)

    assert [(c["t"], c["recommendation"]) for c in changes] == [
        (1, "normal"),
        (3, "throttle"),
        (5, "normal"),
    ]
    assert changes[1]["metrics"] == {"active_jobs": 5}


def test_docker_memory_figures_are_parsed_in_both_unit_systems() -> None:
    assert run._bytes("512MiB") == 512 * 1024**2
    assert run._bytes("1.5GiB") == int(1.5 * 1024**3)
    assert run._bytes("300kB") == 300_000
    assert run._bytes("") == 0
    assert run._percent("12.5%") == 12.5


def test_container_peaks_keep_the_maximum_per_container() -> None:
    samples = run.Samples(
        containers=[
            {"name": "app", "memory_bytes": 10, "cpu_percent": 5.0},
            {"name": "app", "memory_bytes": 30, "cpu_percent": 1.0},
            {"name": "worker", "memory_bytes": 7, "cpu_percent": 90.0},
        ]
    )

    assert run.container_peaks(samples) == {
        "app": {"memory_bytes": 30.0, "cpu_percent": 5.0},
        "worker": {"memory_bytes": 7.0, "cpu_percent": 90.0},
    }


def test_the_closed_loop_stops_submitting_at_a_stop_reason(monkeypatch) -> None:
    monkeypatch.setattr(run.time, "sleep", lambda _s: None)
    api = _FakeApi()
    reasons = iter(["", "", "host load average 9.10 is above 8.0"])

    records, stopped = run.closed_loop(
        api,
        _TARGET,
        jobs=50,
        concurrency=1,
        rows=1,
        timeout=5,
        deadline=float("inf"),
        should_stop=lambda: next(reasons, "host load average 9.10 is above 8.0"),
    )

    assert stopped == "host load average 9.10 is above 8.0"
    assert len(records) == 2  # nothing more was submitted once the host was busy


def test_the_closed_loop_ends_at_its_time_budget(monkeypatch) -> None:
    monkeypatch.setattr(run.time, "sleep", lambda _s: None)
    records, stopped = run.closed_loop(
        _FakeApi(), _TARGET, jobs=50, concurrency=2, rows=1, timeout=5, deadline=0.0
    )

    assert stopped == "time budget reached"
    assert records == []


def test_the_open_loop_stops_at_the_first_throttle(monkeypatch) -> None:
    monkeypatch.setattr(run.time, "sleep", lambda _s: None)
    api = _FakeApi()
    left_normal = iter([False, False, True])

    records, stopped = run.open_loop(
        api,
        _TARGET,
        rate=1000.0,
        seconds=60.0,
        rows=1,
        timeout=5,
        deadline=float("inf"),
        should_stop=lambda: "recommendation left normal" if next(left_normal, True) else "",
    )

    assert stopped == "recommendation left normal"
    assert len(records) == 2


def test_host_pressure_names_load_and_memory(monkeypatch) -> None:
    monkeypatch.setattr(run.os, "getloadavg", lambda: (9.5, 1.0, 1.0))
    assert "load average 9.50" in run.host_pressure(run.HostLimits())

    monkeypatch.setattr(run.os, "getloadavg", lambda: (0.5, 0.5, 0.5))
    monkeypatch.setattr(run, "available_memory_bytes", lambda: 1024**3)
    assert "available memory" in run.host_pressure(run.HostLimits())

    monkeypatch.setattr(run, "available_memory_bytes", lambda: None)  # no /proc/meminfo
    assert run.host_pressure(run.HostLimits()) == ""


def test_backpressure_is_waited_out_and_counted_not_failed(monkeypatch) -> None:
    """Measured on a live Compact: the installation answers 429 while a principal already
    has submissions in preparation. That is its backpressure, not a failed job."""
    monkeypatch.setattr(run.time, "sleep", lambda _s: None)

    class _Busy(_FakeApi):
        def __init__(self) -> None:
            super().__init__()
            self.refusals = 2

        def request(self, method, path, body=None):
            if method == "POST" and self.refusals:
                self.refusals -= 1
                raise run.ApiError("POST /api/workflow-runs -> HTTP 429", status=429, retry_after=2)
            return super().request(method, path, body)

    records, _stopped = run.closed_loop(
        _Busy(), _TARGET, jobs=1, concurrency=1, rows=1, timeout=5, deadline=float("inf")
    )

    (record,) = records
    assert record.status == "completed"
    assert record.admission_rejections == 2
    summary = run.summarize(records, elapsed=1.0)
    assert (summary["accepted"], summary["admission_rejections"], summary["unfinished"]) == (
        1,
        2,
        0,
    )


def test_jobs_are_polled_through_the_compact_status_and_the_cost_is_reported(monkeypatch) -> None:
    """AC17-R155: polling the full job detail made this tool load the API it measured."""
    monkeypatch.setattr(run.time, "sleep", lambda _s: None)
    api = _FakeApi(polls_to_finish=3)

    records, _stopped = run.closed_loop(
        api, _TARGET, jobs=2, concurrency=1, rows=1, timeout=5, deadline=float("inf")
    )

    assert set(api.polls) == {"job-1", "job-2"}  # only the compact form was asked
    polling = run.summarize(records, elapsed=2.0)["polling"]
    assert polling["endpoint"] == run.STATUS_PATH
    assert polling["interval_s"] == run.POLL_INTERVAL_SECONDS
    assert polling["requests"] == 6
    assert polling["requests_per_s"] == 3.0


def test_setup_finds_a_workspace_without_admin_scope() -> None:
    """T1: /api/workspaces needs admin:all, which no UI-created token holds."""

    class _Scoped:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def request(self, method, path, body=None):
            self.calls.append((method, path))
            if path == "/api/workspaces":
                raise run.ApiError("GET /api/workspaces -> HTTP 403", status=403)
            if method == "GET" and path.startswith("/api/projects?"):
                return {"projects": [{"id": "p0", "workspace_id": "ws-1"}]}
            if method == "POST" and path == "/api/projects":
                assert body["workspace_id"] == "ws-1"
                return {"project_id": "p-bench"}
            return {}

    api = _Scoped()
    result = run.setup_project(api, workflow_id="w", concurrency_cap=8)

    assert result["project_id"] == "p-bench"
    assert ("GET", "/api/workspaces") not in api.calls


def _self_signed_localhost(path) -> None:
    """A certificate shaped like the one the installer writes to .ssl/cert.pem."""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def test_a_stands_own_certificate_is_trusted_and_still_verified(tmp_path) -> None:
    """A self-signed stand is reached by trusting its certificate, not by not checking."""
    import ssl

    certificate = tmp_path / "cert.pem"
    _self_signed_localhost(certificate)

    context = run.Api(
        run.Target(
            base_url="https://localhost:8099",
            token="t",
            project_id="",
            workflow_id="",
            ca_file=str(certificate),
        )
    )._context

    assert context is not None
    assert [entry["subject"] for entry in context.get_ca_certs()] == [
        ((("commonName", "localhost"),),)
    ]
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_without_a_certificate_the_system_trust_store_verifies() -> None:
    assert run.Api(_TARGET)._context is None


def test_the_cli_passes_the_certificate_and_offers_no_way_to_skip_verification() -> None:
    from core.entrypoints.cli.ops import profile_benchmark

    parser = profile_benchmark._parser()
    base = ["--base-url", "https://localhost:8099", "--project-id", "p", "--workflow-id", "w"]
    args = parser.parse_args(["closed", *base, "--ca-file", "/stand/.ssl/cert.pem", "--out", "o"])

    assert args.ca_file == "/stand/.ssl/cert.pem"
    for command in ("closed", "sweep"):
        assert parser.parse_args([command, *base, "--out", "o"]).ca_file == ""
    with pytest.raises(SystemExit):
        parser.parse_args(["closed", *base, "--insecure", "--out", "o"])
