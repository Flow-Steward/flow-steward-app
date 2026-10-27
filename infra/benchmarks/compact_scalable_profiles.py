#!/usr/bin/env python3
"""Frozen local process-topology benchmark for Compact and Scalable profiles."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import resource
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASELINE_SHA = "953e9aaf992947a1dc24cab308aa0aad6a6c81e0"
WORKLOAD_VERSION = 1
JOBS_PER_RUN = 24
WARMUP_JOBS = 4


class _DeterministicHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        time.sleep(0.005)
        body = b'{"fixture":"flow-steward-benchmark-v1"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@dataclass(frozen=True)
class Measurement:
    profile: str
    workers: int
    workload: str
    run: int
    jobs: int
    throughput_jobs_s: float
    p50_ms: float
    p95_ms: float
    peak_app_family_kib: int
    cpu_seconds: float
    db_connections_peak: int
    errors: int
    retries: int
    terminal_success: int


def _job(payload: tuple[str, str]) -> float:
    workload, io_url = payload
    started = time.perf_counter()
    if workload == "io":
        with urllib.request.urlopen(io_url, timeout=5) as response:
            if response.read() != b'{"fixture":"flow-steward-benchmark-v1"}':
                raise RuntimeError("deterministic HTTP fixture mismatch")
    elif workload == "cpu":
        value = b"flow-steward-benchmark-v1"
        for _ in range(40_000):
            value = hashlib.sha256(value).digest()
        if len(value) != 32:
            raise RuntimeError("CPU fixture failed")
    elif workload == "subprocess":
        subprocess.run(
            [sys.executable, "-c", "import hashlib; hashlib.sha256(b'fixture').digest()"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        raise ValueError(f"unsupported workload: {workload}")
    return (time.perf_counter() - started) * 1000


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _measure(*, profile: str, workers: int, workload: str, run: int, io_url: str) -> Measurement:
    payload = (workload, io_url)
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
        list(executor.map(_job, [payload] * WARMUP_JOBS))
        started = time.perf_counter()
        errors = 0
        latencies: list[float] = []
        futures = [executor.submit(_job, payload) for _ in range(JOBS_PER_RUN)]
        for future in futures:
            try:
                latencies.append(future.result())
            except Exception:
                errors += 1
        elapsed = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return Measurement(
        profile=profile,
        workers=workers,
        workload=workload,
        run=run,
        jobs=JOBS_PER_RUN,
        throughput_jobs_s=round((JOBS_PER_RUN - errors) / elapsed, 3),
        p50_ms=round(_percentile(latencies, 0.50), 3),
        p95_ms=round(_percentile(latencies, 0.95), 3),
        peak_app_family_kib=int(after.ru_maxrss),
        cpu_seconds=round(
            (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime), 3
        ),
        db_connections_peak=0,
        errors=errors,
        retries=0,
        terminal_success=JOBS_PER_RUN - errors,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DeterministicHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    io_url = f"http://127.0.0.1:{server.server_port}/fixture"
    measurements: list[Measurement] = []
    try:
        for profile in ("compact", "scalable"):
            for workers in (1, 2, 4):
                for workload in ("io", "cpu", "subprocess"):
                    for run in range(1, 4):
                        measurements.append(
                            _measure(
                                profile=profile,
                                workers=workers,
                                workload=workload,
                                run=run,
                                io_url=io_url,
                            )
                        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    payload = {
        "schema_version": 1,
        "baseline_sha": BASELINE_SHA,
        "candidate_sha": args.candidate_sha,
        "workload_version": WORKLOAD_VERSION,
        "jobs_per_run": JOBS_PER_RUN,
        "warmup_jobs": WARMUP_JOBS,
        "cpu_count": os.cpu_count(),
        "resource_budget": "same host; worker count is the only topology variable",
        "limitations": [
            "local process harness; it does not replace full product queue/database acceptance",
            "peak_app_family_kib is OS RUSAGE_CHILDREN high-water evidence",
            "database connections are zero because the frozen harness does not open PostgreSQL",
        ],
        "measurements": [asdict(item) for item in measurements],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if all(item.errors == 0 for item in measurements) else 1


if __name__ == "__main__":
    raise SystemExit(main())
