"""Thin phase-one release-candidate harness controller."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from core.application.release.candidate_cleanup import execute_cleanup, plan_cleanup
from core.application.release.candidate_harness import (
    CandidateFailure,
    CandidateHarness,
    CandidateRunIdentity,
    FailureClass,
    HarnessFailure,
    InfrastructureFailure,
    InputFailure,
    PhaseStatus,
    ProductFailure,
    phase,
)
from core.application.release.candidate_preflight import run_static_preflight
from core.application.release.candidate_requirements import (
    AggregatedRequirements,
    aggregate_requirements,
    load_group_declarations,
)
from core.application.release.candidate_snapshot import SnapshotInputs
from core.application.release.candidate_store import (
    CandidateEvidenceStore,
    CandidateSnapshotStore,
)
from core.infrastructure.release_candidate.docker_gateway import DockerCliGateway
from core.infrastructure.release_candidate.host_gateway import (
    EndpointAllocator,
    EndpointInputError,
    EndpointRegistryError,
    EndpointUnavailableError,
    SubprocessHostInspection,
)

_PATH_FLOW_STEWARD = ".flow-steward"

_EXIT_CODES = {
    FailureClass.INPUT: 2,
    FailureClass.HARNESS: 2,
    FailureClass.INFRASTRUCTURE: 3,
    FailureClass.PRODUCT: 4,
    FailureClass.INCOMPLETE: 5,
}
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


class CandidateHarnessServicePort(Protocol):
    def snapshot(self, args: argparse.Namespace) -> Mapping[str, object]: ...

    def preflight(self, args: argparse.Namespace) -> Mapping[str, object]: ...

    def verify(self, args: argparse.Namespace) -> Mapping[str, object]: ...

    def status(self, args: argparse.Namespace) -> Mapping[str, object]: ...

    def cleanup(self, args: argparse.Namespace) -> Mapping[str, object]: ...


class CandidateHarnessService:
    """Compose application contracts with host adapters; keep parsing in the controller."""

    def snapshot(self, args: argparse.Namespace) -> Mapping[str, object]:
        root = args.root.resolve()
        store = _snapshot_store(root, args.candidate)
        if args.finalize:
            resources = _resource_pairs(args.resource)
            resources["images"] = tuple(sorted((*resources.get("images", ()), *args.image_tag)))
            try:
                finalized = store.finalize(
                    image_digests=_name_value_pairs(args.image_digest),
                    resource_identities=resources,
                    compose_working_dir=args.compose_working_dir,
                    compose_config_files=args.compose_config_files,
                    extension_digest=(
                        _tree_digest(args.extension_dir.resolve()) if args.extension_dir else None
                    ),
                    provenance_checksum=(
                        _file_digest(args.provenance_file.resolve())
                        if args.provenance_file
                        else None
                    ),
                )
            except ValueError as exc:
                raise HarnessFailure(str(exc)) from exc
            return {
                "status": "finalized",
                "candidate_commit": finalized.expected.candidate_commit,
                "fingerprint": finalized.expected.fingerprint,
                "image_digests": dict(finalized.image_digests),
                "extension_digest": finalized.extension_digest,
                "provenance_checksum": finalized.provenance_checksum,
            }

        requirements = _requirements(root, args)
        inputs = _snapshot_inputs(root, args)
        if args.check:
            result = store.check(inputs, requirements=requirements)
            if not result.matches:
                raise HarnessFailure("candidate snapshot is stale: " + "; ".join(result.reasons))
            snapshot = store.load_expected()
            return _snapshot_output(snapshot.to_json(), status="matched", reused=True)

        if store.snapshot_path.exists():
            result = store.check(inputs, requirements=requirements)
            if not result.matches:
                raise HarnessFailure("candidate snapshot is stale: " + "; ".join(result.reasons))
            snapshot = store.load_expected()
            return _snapshot_output(snapshot.to_json(), status="materialized", reused=True)
        snapshot = store.materialize(inputs, requirements=requirements)
        return _snapshot_output(snapshot.to_json(), status="materialized", reused=False)

    def preflight(self, args: argparse.Namespace) -> Mapping[str, object]:
        root = args.root.resolve()
        store = _snapshot_store(root, args.candidate)
        requirements = _requirements(root, args)
        inputs = _snapshot_inputs(root, args, allocate_ports=False)
        comparison = store.check(inputs, requirements=requirements)
        result = run_static_preflight(
            store.load_expected(),
            requirements=requirements,
            host=SubprocessHostInspection(root),
            snapshot_comparison=comparison,
        )
        return {
            "status": "passed",
            "candidate_commit": result.candidate_commit,
            "checks": result.checks,
        }

    def verify(self, args: argparse.Namespace) -> Mapping[str, object]:
        root = args.root.resolve()
        release_dir = (
            args.release_dir.resolve()
            if args.release_dir is not None
            else root / _PATH_FLOW_STEWARD / "release" / args.candidate
        )
        snapshot_store = _snapshot_store(root, args.candidate, release_dir=release_dir)
        try:
            finalized = snapshot_store.load_finalized()
        except (FileNotFoundError, ValueError) as exc:
            raise HarnessFailure("candidate finalized identity is unavailable") from exc
        if finalized.expected.candidate_commit != args.candidate:
            raise HarnessFailure("candidate finalized identity does not match verification input")
        runner = args.runner.resolve()
        if not runner.is_file() or not runner.stat().st_mode & 0o111:
            raise InputFailure("candidate smoke runner is missing or not executable")
        evidence_root = _evidence_root(root, args)
        log_root = release_dir / "runtime" / "verification-logs" / args.run_id
        identity = CandidateRunIdentity(
            candidate_commit=finalized.expected.candidate_commit,
            fingerprint=finalized.expected.fingerprint,
            compose_project=finalized.expected.compose_project,
            run_id=args.run_id,
            image_digests=finalized.image_digests,
        )

        def profile_action(name: str, *runner_args: str):
            def run_profile() -> Path:
                return _run_verification_command(
                    (str(runner), *runner_args),
                    log_path=log_root / f"{name}.log",
                )

            return run_profile

        if args.mode == "safe":
            phases = (phase("safe-profile", profile_action("safe-profile"), check_id="safe"),)
        else:
            phases = (
                phase(
                    "release-profile-1",
                    profile_action("release-profile-1", "--profile", "release"),
                    check_id="release",
                ),
                phase(
                    "release-profile-2",
                    profile_action("release-profile-2", "--profile", "release"),
                    check_id="release",
                ),
            )
        evidence = CandidateHarness(
            identity=identity,
            store=CandidateEvidenceStore(evidence_root),
        ).run(phases)
        failed = next(
            (
                phase_evidence
                for phase_evidence in evidence.phases
                if phase_evidence.status != PhaseStatus.PASSED
            ),
            None,
        )
        if failed is not None:
            reason = failed.reason or f"candidate verification phase {failed.name} did not pass"
            if failed.failure_class is FailureClass.INPUT:
                raise InputFailure(reason)
            if failed.failure_class is FailureClass.HARNESS:
                raise HarnessFailure(reason)
            if failed.failure_class is FailureClass.INFRASTRUCTURE:
                raise InfrastructureFailure(reason)
            raise ProductFailure(reason)
        return {
            "status": "passed",
            "candidate_commit": identity.candidate_commit,
            "fingerprint": identity.fingerprint,
            "compose_project": identity.compose_project,
            "run_id": identity.run_id,
            "phases": [phase_evidence.to_json() for phase_evidence in evidence.phases],
            "evidence_root": str(evidence_root),
        }

    def status(self, args: argparse.Namespace) -> Mapping[str, object]:
        root = args.root.resolve()
        store = _snapshot_store(root, args.candidate)
        if not store.snapshot_path.is_file():
            return {"status": "not-found", "candidate_commit": args.candidate}
        expected = store.load_expected()
        payload: dict[str, object] = {
            "status": "finalized" if store.finalized_path.is_file() else "materialized",
            "candidate_commit": expected.candidate_commit,
            "compose_project": expected.compose_project,
            "fingerprint": expected.fingerprint,
        }
        if store.finalized_path.is_file():
            finalized = store.load_finalized()
            payload["image_digests"] = dict(finalized.image_digests)
            payload["resource_identities"] = {
                kind: list(identities) for kind, identities in finalized.resource_identities.items()
            }
            payload["extension_digest"] = finalized.extension_digest
            payload["provenance_checksum"] = finalized.provenance_checksum
        evidence = CandidateEvidenceStore(_evidence_root(root, args)).verified_manifest(
            args.candidate,
            run_id=args.run_id,
        )
        payload["evidence_verified"] = evidence is not None
        if evidence is not None:
            payload["run_id"] = evidence["run_id"]
            payload["evidence_phases"] = evidence["phases"]
        return payload

    def cleanup(self, args: argparse.Namespace) -> Mapping[str, object]:
        root = args.root.resolve()
        release_root = root / _PATH_FLOW_STEWARD / "release"
        store = _snapshot_store(root, args.candidate)
        try:
            finalized = store.load_finalized()
        except (FileNotFoundError, ValueError) as exc:
            raise HarnessFailure("candidate finalized identity is unavailable") from exc
        gateway = DockerCliGateway()
        evidence = CandidateEvidenceStore(_evidence_root(root, args)).verified_manifest(
            args.candidate,
            run_id=args.run_id,
        )
        if evidence is None:
            raise HarnessFailure("candidate external evidence is not verified")
        evidence_verified = True
        try:
            plan = plan_cleanup(
                finalized,
                gateway.inventory(),
                preserve_sha=args.preserve_sha,
                release_root=release_root,
                external_evidence_verified=evidence_verified,
            )
            execute_cleanup(
                plan,
                gateway=gateway,
                remove_release_directory=_remove_release_directory,
            )
            if args.preserve_sha != args.candidate:
                EndpointAllocator(release_root / "endpoint-state").release(args.candidate)
        except EndpointInputError as exc:
            raise InputFailure(str(exc)) from exc
        except EndpointRegistryError as exc:
            raise HarnessFailure(str(exc)) from exc
        except RuntimeError as exc:
            raise InfrastructureFailure(str(exc)) from exc
        return {
            "status": "cleaned",
            "candidate_commit": args.candidate,
            "removed": {
                "containers": len(plan.containers),
                "networks": len(plan.networks),
                "volumes": len(plan.volumes),
                "images": len(plan.images),
                "release_directories": len(plan.release_directories),
            },
            "external_evidence_verified": evidence_verified,
            "run_id": evidence["run_id"],
        }


def main(
    argv: Sequence[str] | None = None,
    *,
    service: CandidateHarnessServicePort | None = None,
) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.command == "cleanup" and not args.confirm:
        print("candidate cleanup requires --confirm", file=sys.stderr)
        return 2
    selected_service = service or CandidateHarnessService()
    try:
        payload = getattr(selected_service, args.command)(args)
    except CandidateFailure as exc:
        failure_class = exc.failure_class
        print(
            json.dumps(
                {"status": "failed", "failure_class": failure_class.value, "reason": str(exc)},
                sort_keys=True,
            )
        )
        return _EXIT_CODES[failure_class]
    except FileNotFoundError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "failure_class": FailureClass.INPUT.value,
                    "reason": f"required candidate file is missing: {Path(exc.filename or '').name}",
                },
                sort_keys=True,
            )
        )
        return _EXIT_CODES[FailureClass.INPUT]
    except ValueError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "failure_class": FailureClass.HARNESS.value,
                    "reason": str(exc),
                },
                sort_keys=True,
            )
        )
        return _EXIT_CODES[FailureClass.HARNESS]
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "failure_class": FailureClass.INCOMPLETE.value,
                    "reason": "interrupted",
                },
                sort_keys=True,
            )
        )
        return _EXIT_CODES[FailureClass.INCOMPLETE]
    print(json.dumps(payload, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot")
    _common_arguments(snapshot, source=True)
    mode = snapshot.add_mutually_exclusive_group(required=True)
    mode.add_argument("--materialize", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--finalize", action="store_true")
    _snapshot_arguments(snapshot)

    preflight = subparsers.add_parser("preflight")
    _common_arguments(preflight, source=True)
    _snapshot_arguments(preflight)

    verify = subparsers.add_parser("verify")
    _common_arguments(verify, source=False)
    verify.add_argument("--release-dir", type=Path)
    verify.add_argument("--evidence-root", type=Path)
    verify.add_argument("--run-id", required=True)
    verify.add_argument("--mode", choices=("safe", "release"), required=True)
    verify.add_argument("--runner", type=Path, required=True)

    status = subparsers.add_parser("status")
    _common_arguments(status, source=False)
    _evidence_arguments(status)

    cleanup = subparsers.add_parser("cleanup")
    _common_arguments(cleanup, source=False)
    cleanup.add_argument("--confirm", action="store_true")
    cleanup.add_argument("--preserve-sha")
    _evidence_arguments(cleanup)
    return parser


def _common_arguments(parser: argparse.ArgumentParser, *, source: bool) -> None:
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--candidate", required=True)
    if source:
        parser.add_argument("--source-env", type=Path)
        parser.add_argument("--profiles", default="oss,tenant,deploy")
        parser.add_argument("--check-id", action="append", default=[])


def _snapshot_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--app-port", type=int)
    parser.add_argument("--http-port", type=int)
    parser.add_argument("--mailpit-port", type=int)
    parser.add_argument("--image-digest", action="append", default=[])
    parser.add_argument("--image-tag", action="append", default=[])
    parser.add_argument("--resource", action="append", default=[])
    parser.add_argument("--compose-working-dir")
    parser.add_argument("--compose-config-files")
    parser.add_argument("--extension-dir", type=Path)
    parser.add_argument("--provenance-file", type=Path)


def _requirements(root: Path, args: argparse.Namespace) -> AggregatedRequirements:
    profiles = {value for value in str(args.profiles).split(",") if value}
    declarations = load_group_declarations(root)
    return aggregate_requirements(
        root / "scripts" / "smoke" / "checks.json",
        declarations,
        profiles=profiles,
        check_ids=set(args.check_id) or None,
    )


def _snapshot_inputs(
    root: Path,
    args: argparse.Namespace,
    *,
    allocate_ports: bool = True,
) -> SnapshotInputs:
    source_env = (args.source_env or root / ".env.prod").resolve()
    ports = (args.app_port, args.http_port, args.mailpit_port)
    if any(port is not None for port in ports) and not all(port is not None for port in ports):
        raise InputFailure("candidate endpoint pins must provide all three ports")
    if all(port is not None for port in ports):
        selected_ports = tuple(int(port) for port in ports if port is not None)
    elif allocate_ports:
        try:
            selected_ports = EndpointAllocator(
                root / _PATH_FLOW_STEWARD / "release" / "endpoint-state"
            ).allocate(
                args.candidate,
                count=3,
            )
        except EndpointInputError as exc:
            raise InputFailure(str(exc)) from exc
        except EndpointRegistryError as exc:
            raise HarnessFailure(str(exc)) from exc
        except EndpointUnavailableError as exc:
            raise InfrastructureFailure(str(exc)) from exc
    else:
        expected = _snapshot_store(root, args.candidate).load_expected()
        selected_ports = tuple(
            int(expected.plain_inputs[name])
            for name in ("APP_PORT", "HTTP_PORT", "MAILPIT_HTTP_PORT")
        )
    source_paths = {
        "app_dockerfile": root / "infra" / "docker" / "Dockerfile",
        "mcp_dockerfile": root / "infra" / "mcp" / "Dockerfile",
        "production_compose": root / "infra" / "docker" / "docker-compose.prod.yaml",
        "smoke_manifest": root / "scripts" / "smoke" / "checks.json",
    }
    return SnapshotInputs(
        candidate_commit=args.candidate,
        compose_project=f"fs-candidate-{args.candidate[:12]}",
        source_env=source_env,
        plain_values={
            "APP_PORT": str(selected_ports[0]),
            "HTTP_PORT": str(selected_ports[1]),
            "MAILPIT_HTTP_PORT": str(selected_ports[2]),
        },
        source_paths=source_paths,
        image_inputs={
            "fs-app": ("app_dockerfile",),
            "fs-cli": ("app_dockerfile",),
            "fs-mcp": ("mcp_dockerfile",),
        },
    )


def _snapshot_store(
    root: Path,
    candidate_commit: str,
    *,
    release_dir: Path | None = None,
) -> CandidateSnapshotStore:
    if not _FULL_SHA.fullmatch(candidate_commit):
        raise InputFailure("candidate must be a full lowercase Git SHA")
    return CandidateSnapshotStore(
        release_dir or root / _PATH_FLOW_STEWARD / "release" / candidate_commit
    )


def _evidence_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--run-id")


def _evidence_root(root: Path, args: argparse.Namespace) -> Path:
    configured = getattr(args, "evidence_root", None)
    return (
        configured.resolve() if configured is not None else root / "logs" / "release" / "evidence"
    )


def _run_verification_command(command: Sequence[str], *, log_path: Path) -> Path:
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    process: subprocess.Popen[str] | None = None
    try:
        with log_path.open("w", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert process.stdout is not None
            for line in process.stdout:
                log_handle.write(line)
                log_handle.flush()
                print(line, end="")
            return_code = process.wait()
    except KeyboardInterrupt as exc:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()
        object.__setattr__(exc, "log_paths", (log_path,))
        raise
    if return_code != 0:
        raise ProductFailure(
            f"candidate verification command failed with exit code {return_code}",
            log_paths=(log_path,),
        )
    return log_path


def _snapshot_output(
    payload: Mapping[str, object],
    *,
    status: str,
    reused: bool,
) -> dict[str, object]:
    return {
        "status": status,
        "reused": reused,
        "candidate_commit": payload["candidate_commit"],
        "compose_project": payload["compose_project"],
        "fingerprint": payload["fingerprint"],
        "plain_inputs": payload["plain_inputs"],
    }


def _name_value_pairs(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, item = value.partition("=")
        if not separator or not name or not item:
            raise InputFailure("candidate name/value argument must use NAME=VALUE")
        result[name] = item
    return result


def _resource_pairs(values: Sequence[str]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for value in values:
        kind, separator, identity = value.partition("=")
        if not separator or kind not in {"containers", "networks", "volumes", "images"}:
            raise InputFailure("candidate resource must use KIND=IDENTITY")
        grouped.setdefault(kind, []).append(identity)
    return {kind: tuple(identities) for kind, identities in grouped.items()}


def _tree_digest(root: Path) -> str:
    if not root.is_dir():
        raise InputFailure("candidate extension directory is missing")
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _file_digest(path: Path) -> str:
    if not path.is_file():
        raise InputFailure("candidate provenance file is missing")
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _remove_release_directory(path: Path) -> None:
    shutil.rmtree(path)


if __name__ == "__main__":
    raise SystemExit(main())
