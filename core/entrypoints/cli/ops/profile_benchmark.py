"""AC19 profile benchmark: `prepare` inside the app container, `closed`/`sweep` on the host.

    # inside fs-app (Scalable) or compact-runtime (Compact)
    python -m core.entrypoints.cli.ops.profile_benchmark prepare --steps 5

    # on the Docker host, from the checkout, standard library only
    FS_BENCHMARK_TOKEN=<personal access token> python3 -m \\
        core.entrypoints.cli.ops.profile_benchmark closed \\
        --base-url https://fs.example.test --project-id <id> --workflow-id <id> \\
        --jobs 200 --concurrency 8 --rows 200 --compose-project <project> --out run.json

The token is read from FS_BENCHMARK_TOKEN, never from the command line. A personal
access token created in the UI is enough with jobs.read, jobs.write, projects.read and
projects.write: setup creates the project and links the workflow (projects.*), runs
submit and poll jobs (jobs.*), and the workload sampler reads /api/workload (jobs.read).
Only `cleanup`, which deletes the project, needs an administrator's admin:all.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_CA_FILE_HELP = "trust this certificate, e.g. the installation's own .ssl/cert.pem"


def _prepare(args: argparse.Namespace) -> int:
    from core.application.auth.config import DEFAULT_TENANT_ID
    from core.application.management.scope import normalize_scope
    from core.application.registry.db_store import RegistryDBStore
    from core.application.tooling.ops.profile_benchmark_prepare import prepare_benchmark_workflow
    from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID
    from core.infrastructure.config.runtime_env import runtime_env

    dsn = str(runtime_env.get("FS_DB_DSN") or runtime_env.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("prepare needs FS_DB_DSN; run it inside the application container", file=sys.stderr)
        return 2
    account_id = str(runtime_env.get("FS_DEFAULT_ACCOUNT_ID") or DEFAULT_ACCOUNT_ID).strip()
    store = RegistryDBStore(dsn)
    if args.cleanup:
        from core.application.tooling.ops.profile_benchmark_prepare import (
            remove_benchmark_workflows,
        )

        print(json.dumps({"removed": remove_benchmark_workflows(store=store)}, sort_keys=True))
        return 0
    result = prepare_benchmark_workflow(
        store=store,
        scope=normalize_scope({"account_id": account_id, "tenant_id": DEFAULT_TENANT_ID}),
        steps=args.steps,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def _target(args: argparse.Namespace):
    from core.application.tooling.ops.profile_benchmark_run import Target
    from core.infrastructure.config.runtime_env import runtime_env

    token = str(runtime_env.get("FS_BENCHMARK_TOKEN") or "").strip()
    if not token:
        raise SystemExit("set FS_BENCHMARK_TOKEN to a personal access token of an operator")
    return Target(
        base_url=args.base_url,
        token=token,
        project_id=args.project_id,
        workflow_id=args.workflow_id,
        ca_file=args.ca_file,
        compose_project=args.compose_project,
    )


def _write(path: str, payload: dict) -> None:
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _limits(run, args: argparse.Namespace):
    return run.HostLimits(
        max_load_average=args.max_load,
        min_available_bytes=int(args.min_available_gib * 1024**3),
    )


def _facts(run, api, target) -> dict[str, Any]:
    return {"host": run.host_facts(), "installation": run.installation_facts(api, target)}


def _closed(args: argparse.Namespace) -> int:
    from core.application.tooling.ops import profile_benchmark_run as run

    target = _target(args)
    api = run.Api(target)
    samples = run.Samples()
    before = _facts(run, api, target)
    pressure = run.host_pressure(_limits(run, args))
    if pressure:
        print(f"not starting: {pressure}", file=sys.stderr)
        return 3
    started = time.monotonic()
    with run.Sampler(api, target, samples, limits=_limits(run, args)) as sampler:
        records, stopped = run.closed_loop(
            api,
            target,
            jobs=args.jobs,
            concurrency=args.concurrency,
            rows=args.rows,
            timeout=args.job_timeout,
            deadline=started + args.budget,
            should_stop=lambda: sampler.pressure,
        )
    summary = run.summarize(records, elapsed=time.monotonic() - started)
    summary["stopped_early"] = stopped
    payload = {
        "mode": "closed",
        "parameters": {k: v for k, v in vars(args).items() if k != "handler"},
        "before": before,
        "summary": summary,
        "workload_transitions": run.workload_transitions(samples),
        "container_peaks": run.container_peaks(samples),
        "samples": {"workload": samples.workload, "containers": samples.containers},
    }
    _write(args.out, payload)
    print(json.dumps({"summary": summary, "container_peaks": payload["container_peaks"]}, indent=2))
    return 0 if summary["unfinished"] == 0 else 1


def _sweep(args: argparse.Namespace) -> int:
    from core.application.tooling.ops import profile_benchmark_run as run

    target = _target(args)
    api = run.Api(target)
    steps: list[dict[str, Any]] = []
    sweep_deadline = time.monotonic() + args.budget
    for rate in (float(item) for item in args.rates.split(",") if item.strip()):
        before = _facts(run, api, target)
        pressure = run.host_pressure(_limits(run, args))
        if pressure:
            print(f"stopping the sweep: {pressure}", file=sys.stderr)
            break
        samples = run.Samples()
        started = time.monotonic()
        with run.Sampler(api, target, samples, limits=_limits(run, args)) as sampler:
            records, stopped = run.open_loop(
                api,
                target,
                rate=rate,
                seconds=args.seconds,
                rows=args.rows,
                timeout=args.job_timeout,
                deadline=sweep_deadline,
                should_stop=lambda s=sampler: (
                    s.pressure or ("recommendation left normal" if s.left_normal.is_set() else "")
                ),
            )
        summary = run.summarize(records, elapsed=time.monotonic() - started)
        summary["stopped_early"] = stopped
        transitions = run.workload_transitions(samples)
        steps.append(
            {
                "rate_jobs_per_s": rate,
                "before": before,
                "summary": summary,
                "workload_transitions": transitions,
                "container_peaks": run.container_peaks(samples),
                "samples": {"workload": samples.workload, "containers": samples.containers},
            }
        )
        print(
            json.dumps(
                {
                    "rate": rate,
                    "summary": summary,
                    "recommendations": [item["recommendation"] for item in transitions],
                }
            ),
            flush=True,
        )
        if stopped:
            # The auditor's rule: stop at the first throttle, and on host pressure or the
            # budget, rather than pushing the next rate.
            break
    _write(
        args.out,
        {
            "mode": "sweep",
            "parameters": {k: v for k, v in vars(args).items() if k != "handler"},
            "steps": steps,
        },
    )
    return 0


def _connection(args: argparse.Namespace):
    from core.application.tooling.ops.profile_benchmark_run import Api, Target
    from core.infrastructure.config.runtime_env import runtime_env

    token = str(runtime_env.get("FS_BENCHMARK_TOKEN") or "").strip()
    if not token:
        raise SystemExit("set FS_BENCHMARK_TOKEN to a personal access token of an operator")
    return Api(
        Target(
            base_url=args.base_url,
            token=token,
            project_id="",
            workflow_id="",
            ca_file=args.ca_file,
        )
    )


def _setup(args: argparse.Namespace) -> int:
    from core.application.tooling.ops.profile_benchmark_run import setup_project

    result = setup_project(
        _connection(args),
        workflow_id=args.workflow_id,
        concurrency_cap=args.project_concurrency,
        workspace_id=args.workspace_id,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def _cleanup(args: argparse.Namespace) -> int:
    from core.application.tooling.ops.profile_benchmark_run import cleanup_project

    print(json.dumps(cleanup_project(_connection(args), project_id=args.project_id)))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="profile_benchmark")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="create the benchmark workflow (in-container)")
    prepare.add_argument("--steps", type=int, default=5)
    prepare.add_argument(
        "--cleanup", action="store_true", help="remove the benchmark workflows it created"
    )
    prepare.set_defaults(handler=_prepare)
    setup = commands.add_parser("setup", help="create the benchmark project (host, API)")
    setup.add_argument("--base-url", required=True)
    setup.add_argument("--workflow-id", required=True)
    setup.add_argument("--workspace-id", default="")
    setup.add_argument("--project-concurrency", type=int, default=64)
    setup.add_argument(
        "--ca-file",
        default="",
        help=_CA_FILE_HELP,
    )
    setup.set_defaults(handler=_setup)
    cleanup = commands.add_parser("cleanup", help="remove the benchmark project (host, API)")
    cleanup.add_argument("--base-url", required=True)
    cleanup.add_argument("--project-id", required=True)
    cleanup.add_argument(
        "--ca-file",
        default="",
        help=_CA_FILE_HELP,
    )
    cleanup.set_defaults(handler=_cleanup)
    for name, handler in (("closed", _closed), ("sweep", _sweep)):
        command = commands.add_parser(name)
        command.add_argument("--base-url", required=True)
        command.add_argument("--project-id", required=True)
        command.add_argument("--workflow-id", required=True)
        command.add_argument("--rows", type=int, default=200)
        command.add_argument("--job-timeout", type=float, default=600.0)
        command.add_argument("--compose-project", default="")
        command.add_argument(
            "--ca-file",
            default="",
            help=_CA_FILE_HELP,
        )
        command.add_argument("--out", required=True)
        command.add_argument(
            "--budget", type=float, default=600.0, help="seconds this cell may take at most"
        )
        command.add_argument(
            "--max-load", type=float, default=8.0, help="stop above this 1-minute load average"
        )
        command.add_argument(
            "--min-available-gib",
            type=float,
            default=2.0,
            help="stop below this much available host memory",
        )
        command.set_defaults(handler=handler)
    closed = commands.choices["closed"]
    closed.add_argument("--jobs", type=int, default=100)
    closed.add_argument("--concurrency", type=int, default=4)
    sweep = commands.choices["sweep"]
    sweep.add_argument("--rates", default="0.5,1,2,4", help="comma-separated jobs per second")
    sweep.add_argument("--seconds", type=float, default=60.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
