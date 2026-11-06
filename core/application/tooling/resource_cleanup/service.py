"""Fail-closed cleanup policy over an explicit, locked ownership ledger."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from core.application.tooling.resource_cleanup.models import (
    KINDS,
    CleanupReceipt,
    Ledger,
    Resource,
    validate_owner,
)

ORDER = {"container": 0, "network": 1, "volume": 2, "image_tag": 3, "builder": 4, "directory": 5}


class JournalPort(Protocol):
    root: Path

    def locked(self) -> AbstractContextManager[None]: ...
    def path(self, owner: str) -> Path: ...
    def workspace(self, owner: str, token: str) -> Path: ...
    def load(self, owner: str) -> Ledger: ...
    def save(self, ledger: Ledger) -> None: ...
    def peers(self) -> list[Ledger]: ...
    def receipt(self, owner: str, payload: dict) -> None: ...


class ResourcesPort(Protocol):
    def inventory(self) -> dict[str, list[str]]: ...
    def create_directory(self, ledger: Ledger, name: str) -> tuple[str, dict[str, str]]: ...
    def capture(
        self, ledger: Ledger, kind: str, identity: str, *, borrowed: bool = False
    ) -> dict[str, str]: ...
    def validate(self, ledger: Ledger, resource: Resource) -> bool: ...
    def remove(self, ledger: Ledger, resource: Resource) -> None: ...
    def finalize_workspace(self, ledger: Ledger) -> None: ...
    def discover(self, ledger: Ledger) -> list[tuple[str, str]]: ...
    def process_alive(self, pid: int) -> bool: ...
    def process_group_alive(self, pgid: int) -> bool: ...
    def current_pid(self) -> int: ...
    def execution_environment(self) -> dict[str, str]: ...
    def execute_owned(
        self, command: list[str], environment: dict[str, str], on_start: Callable[[int], None]
    ) -> int: ...
    def free_bytes(self, path: Path) -> int: ...
    def create_builder(self, ledger: Ledger) -> tuple[str, dict[str, str]]: ...
    def prepare_environment(
        self,
        ledger: Ledger,
        command: list[str],
        environment: dict[str, str],
        scratch: str,
        cache: str,
    ) -> dict[str, str]: ...


def _resource(ledger: Ledger, resource_id: str) -> Resource:
    for resource in ledger.resources:
        if resource.resource_id == resource_id and resource.state != "removed":
            return resource
    raise ValueError("unknown live resource ID")


def _claims(resource: Resource, peers: list[Ledger], owner: str) -> Iterator[str]:
    for peer in peers:
        if peer.owner == owner:
            continue
        for other in peer.resources:
            if other.state not in {"active", "retained"}:
                continue
            same = resource.kind == other.kind and resource.identity == other.identity
            if resource.kind == other.kind == "image_tag":
                same = same or bool(
                    resource.proof.get("native_id")
                    and resource.proof.get("daemon_id")
                    and resource.proof.get("native_id") == other.proof.get("native_id")
                    and resource.proof.get("daemon_id") == other.proof.get("daemon_id")
                )
            if resource.kind == other.kind == "directory":
                left, right = Path(resource.identity), Path(other.identity)
                same = left.is_relative_to(right) or right.is_relative_to(left)
            if same:
                yield peer.owner


class ResourceCleanup:
    def __init__(self, journal: JournalPort, native: ResourcesPort) -> None:
        self.journal = journal
        self.native = native

    def initialize(self, owner: str) -> Ledger:
        validate_owner(owner)
        with self.journal.locked():
            if self.journal.path(owner).exists():
                return self.journal.load(owner)
            token = uuid4().hex
            ledger = Ledger(
                owner,
                token,
                str(self.journal.workspace(owner, token)),
                datetime.now(UTC).isoformat(),
            )
            self.journal.save(ledger)
            return ledger

    def create_directory(self, owner: str, name: str) -> Resource:
        if (
            not name
            or name in {".", ".."}
            or Path(name).name != name
            or "/" in name
            or "\\" in name
        ):
            raise ValueError("directory name must be one safe component")
        with self.journal.locked():
            ledger = self.journal.load(owner)
            resource = Resource(
                uuid4().hex, "directory", str(Path(ledger.workspace) / name), {}, purpose=name
            )
            ledger.resources.append(resource)
            self.journal.save(ledger)
            resource.identity, resource.proof = self.native.create_directory(ledger, name)
            self.journal.save(ledger)
            return resource

    def track(self, owner: str, kind: str, identity: str, *, borrowed: bool = False) -> Resource:
        if kind not in KINDS:
            raise ValueError("unsupported resource kind")
        with self.journal.locked():
            ledger = self.journal.load(owner)
            proof = self.native.capture(ledger, kind, identity, borrowed=borrowed)
            for existing in ledger.resources:
                if (
                    existing.kind != kind
                    or existing.identity != identity
                    or existing.state == "removed"
                ):
                    continue
                if (
                    borrowed
                    and not existing.disposable
                    and existing.state == "released"
                    and existing.proof == proof
                ):
                    existing.state = "active"
                    self.journal.save(ledger)
                    return existing
                raise ValueError("resource already registered")
            resource = Resource(uuid4().hex, kind, identity, proof, disposable=not borrowed)
            ledger.resources.append(resource)
            self.journal.save(ledger)
            return resource

    def create_builder(self, owner: str) -> Resource:
        with self.journal.locked():
            ledger = self.journal.load(owner)
            if any(r.kind == "builder" and r.state != "removed" for r in ledger.resources):
                raise ValueError("task already has a private builder")
            # Persist intent before the native side effect, so failures remain visible.
            resource = Resource(uuid4().hex, "builder", "fs-owned-" + ledger.token, {})
            ledger.resources.append(resource)
            self.journal.save(ledger)
            resource.identity, resource.proof = self.native.create_builder(ledger)
            self.journal.save(ledger)
            return resource

    def reconcile_created(self, owner: str) -> None:
        """Recover labelled output even if the child exited before explicit registration."""
        with self.journal.locked():
            ledger = self.journal.load(owner)
            known = {(r.kind, r.identity) for r in ledger.resources if r.state != "removed"}
            for kind, identity in self.native.discover(ledger):
                if (kind, identity) in known:
                    continue
                proof = self.native.capture(ledger, kind, identity)
                ledger.resources.append(Resource(uuid4().hex, kind, identity, proof))
                known.add((kind, identity))
                self.journal.save(ledger)

    def retain(
        self,
        owner: str,
        resource_id: str,
        *,
        consumer: str,
        reason: str,
        release_event: str,
        review_at: str,
    ) -> None:
        if not all((consumer, reason, release_event, review_at)):
            raise ValueError("retention requires consumer, reason, release event and review time")
        datetime.fromisoformat(review_at.replace("Z", "+00:00"))
        with self.journal.locked():
            ledger = self.journal.load(owner)
            resource = _resource(ledger, resource_id)
            resource.state = "retained"
            resource.consumers = sorted(set(resource.consumers) | {consumer})
            resource.retention = {
                "reason": reason,
                "release_event": release_event,
                "review_at": review_at,
            }
            self.journal.save(ledger)

    def release(self, owner: str, resource_id: str, *, consumer: str | None = None) -> Resource:
        with self.journal.locked():
            ledger = self.journal.load(owner)
            resource = _resource(ledger, resource_id)
            if consumer is not None:
                if consumer not in resource.consumers:
                    raise ValueError("unknown retained consumer")
                resource.consumers.remove(consumer)
            if resource.consumers:
                if consumer is None:
                    raise ValueError("resource still has an active consumer")
                self.journal.save(ledger)
                return resource
            resource.state = "released"
            resource.retention = {}
            self.journal.save(ledger)
            return resource

    def cleanup(self, owner: str, *, apply: bool = False) -> CleanupReceipt:
        with self.journal.locked():
            ledger = self.journal.load(owner)
            peers = self.journal.peers()
            receipt = CleanupReceipt(
                owner, apply, free_bytes_before=self.native.free_bytes(self.journal.root)
            )
            try:
                receipt.inventory_before = self.native.inventory()
            except (ValueError, OSError) as exc:
                receipt.blocked["inventory"] = str(exc)
                if apply:
                    self.journal.receipt(owner, asdict(receipt))
                return receipt
            planned, removed, retained = [], [], []
            live = bool(
                (ledger.run_pid and self.native.process_alive(ledger.run_pid))
                or (ledger.run_pgid and self.native.process_group_alive(ledger.run_pgid))
            )
            for resource in sorted(ledger.resources, key=lambda r: ORDER[r.kind]):
                if resource.state == "removed":
                    continue
                if not resource.disposable:
                    retained.append(resource.resource_id)
                    continue
                if (
                    resource.state == "retained"
                    and resource.consumers
                    and all(
                        resource.retention.get(key)
                        for key in ("reason", "release_event", "review_at")
                    )
                ):
                    retained.append(resource.resource_id)
                    continue
                if live or resource.state != "released" or resource.consumers:
                    receipt.blocked[resource.resource_id] = (
                        "active execution lease or resource not released"
                    )
                    continue
                if tuple(_claims(resource, peers, owner)):
                    receipt.blocked[resource.resource_id] = "another ledger has an active consumer"
                    continue
                try:
                    exists = self.native.validate(ledger, resource)
                    planned.append(resource.resource_id)
                    if apply:
                        if exists:
                            self.native.remove(ledger, resource)
                        resource.state = "removed"
                        self.journal.save(ledger)
                        removed.append(resource.resource_id)
                except (ValueError, OSError) as exc:
                    receipt.blocked[resource.resource_id] = str(exc)
            if (
                apply
                and not live
                and all(r.state == "removed" or not r.disposable for r in ledger.resources)
            ):
                try:
                    self.native.finalize_workspace(ledger)
                except (ValueError, OSError) as exc:
                    receipt.blocked["workspace"] = str(exc)
            receipt.planned = tuple(planned)
            receipt.removed = tuple(removed)
            receipt.retained = tuple(retained)
            try:
                receipt.inventory_after = self.native.inventory()
                expected: dict[str, set[str]] = {}
                allowed_additions: dict[str, set[str]] = {}
                for resource in ledger.resources:
                    if resource.resource_id not in removed:
                        continue
                    category = {
                        "container": "containers",
                        "network": "networks",
                        "volume": "volumes",
                        "image_tag": "images",
                        "builder": "builders",
                    }.get(resource.kind)
                    if category is None:
                        continue
                    identity = resource.identity
                    if resource.kind == "image_tag":
                        identity += "|" + resource.proof["native_id"]
                        allowed_additions.setdefault("images", set()).add(
                            "<none>:<none>|" + resource.proof["native_id"]
                        )
                    expected.setdefault(category, set()).add(identity)
                    if resource.kind == "builder":
                        expected.setdefault("containers", set()).add(resource.proof["container_id"])
                        expected.setdefault("volumes", set()).add(resource.proof["volume_name"])
                for category in receipt.inventory_before.keys() | receipt.inventory_after.keys():
                    before = set(receipt.inventory_before.get(category, []))
                    after = set(receipt.inventory_after.get(category, []))
                    deleted, added = before - after, after - before
                    if deleted or added:
                        receipt.inventory_changes[category] = {
                            "removed": sorted(deleted),
                            "added": sorted(added),
                        }
                    if deleted - expected.get(category, set()) or added - allowed_additions.get(
                        category, set()
                    ):
                        receipt.blocked["inventory"] = (
                            "unexplained Docker inventory change; inspect receipt before completion"
                        )
            except (ValueError, OSError) as exc:
                receipt.blocked["inventory"] = str(exc)
            receipt.free_bytes_after = self.native.free_bytes(self.journal.root)
            if apply:
                self.journal.receipt(owner, asdict(receipt))
            return receipt
