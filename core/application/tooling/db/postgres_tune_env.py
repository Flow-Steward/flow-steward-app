#!/usr/bin/env python3
"""Emit a small pgtune-style env block for this project's Postgres compose config."""

from __future__ import annotations

import argparse
import math
import os
from typing import Any


def _detect_memory_mb() -> int:
    page_size = os.sysconf("SC_PAGE_SIZE")
    phys_pages = os.sysconf("SC_PHYS_PAGES")
    total_bytes = page_size * phys_pages
    return max(512, total_bytes // (1024 * 1024))


def _round_mb(value_mb: float, step_mb: int = 16) -> int:
    rounded = int(math.floor(value_mb / step_mb) * step_mb)
    return max(step_mb, rounded)


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _fmt_mb(value_mb: int) -> str:
    if value_mb % 1024 == 0:
        return f"{value_mb // 1024}GB"
    return f"{value_mb}MB"


def build_profile(memory_mb: int, max_connections: int, storage: str) -> dict[str, str]:
    shared_buffers_mb = _clamp(_round_mb(memory_mb * 0.25), 128, 8192)
    effective_cache_size_mb = _clamp(_round_mb(memory_mb * 0.75), 512, 32768)
    maintenance_work_mem_mb = _clamp(_round_mb(memory_mb * 0.05), 64, 2048)

    work_mem_budget_mb = (memory_mb * 0.25) / max(max_connections * 3, 1)
    work_mem_mb = _clamp(math.floor(work_mem_budget_mb), 4, 64)

    max_wal_size_mb = _clamp(_round_mb(max(memory_mb * 0.125, 2048), step_mb=256), 2048, 16384)
    min_wal_size_mb = _clamp(max_wal_size_mb // 4, 512, 4096)

    if storage == "ssd":
        random_page_cost = "1.1"
        effective_io_concurrency = "200"
    else:
        random_page_cost = "2.5"
        effective_io_concurrency = "2"

    return {
        "POSTGRES_SHARED_BUFFERS": _fmt_mb(shared_buffers_mb),
        "POSTGRES_EFFECTIVE_CACHE_SIZE": _fmt_mb(effective_cache_size_mb),
        "POSTGRES_WORK_MEM": f"{work_mem_mb}MB",
        "POSTGRES_MAINTENANCE_WORK_MEM": _fmt_mb(maintenance_work_mem_mb),
        "POSTGRES_RANDOM_PAGE_COST": random_page_cost,
        "POSTGRES_EFFECTIVE_IO_CONCURRENCY": effective_io_concurrency,
        "POSTGRES_CHECKPOINT_COMPLETION_TARGET": "0.9",
        "POSTGRES_WAL_COMPRESSION": "on",
        "POSTGRES_MAX_WAL_SIZE": _fmt_mb(max_wal_size_mb),
        "POSTGRES_MIN_WAL_SIZE": _fmt_mb(min_wal_size_mb),
        "POSTGRES_DEFAULT_STATISTICS_TARGET": "200",
        "POSTGRES_JIT": "off",
    }


def generate_tuned_env_profile(
    *,
    memory_gb: float | None = None,
    max_connections: int = 100,
    storage: str = "ssd",
) -> dict[str, Any]:
    """Return a structured pgtune-style environment profile."""
    memory_mb = int(memory_gb * 1024) if memory_gb is not None else _detect_memory_mb()
    profile = build_profile(
        memory_mb=memory_mb,
        max_connections=max_connections,
        storage=storage,
    )
    return {
        "memory_mb": memory_mb,
        "max_connections": int(max_connections),
        "storage": str(storage),
        "profile": profile,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--memory-gb",
        type=float,
        default=None,
        help="Total RAM to size against. Defaults to detected host memory.",
    )
    parser.add_argument(
        "--max-connections",
        type=int,
        default=100,
        help="Target max_connections for the work_mem heuristic.",
    )
    parser.add_argument(
        "--storage",
        choices=("ssd", "hdd"),
        default="ssd",
        help="Storage profile for random_page_cost/effective_io_concurrency.",
    )
    args = parser.parse_args()

    result = generate_tuned_env_profile(
        memory_gb=args.memory_gb,
        max_connections=args.max_connections,
        storage=args.storage,
    )

    print(
        "# Postgres tuned env block\n"
        f"# memory={result['memory_mb']}MB "
        f"max_connections={result['max_connections']} storage={result['storage']}"
    )
    for key, value in result["profile"].items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
