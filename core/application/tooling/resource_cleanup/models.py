"""Validated, secret-free ownership records for disposable agent resources."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

KINDS = frozenset({"directory", "container", "network", "volume", "image_tag", "builder"})
STATES = frozenset({"active", "retained", "released", "removed"})
OWNER_LABEL = "org.flowsteward.task"
TOKEN_LABEL = "org.flowsteward.resource-token"
DISPOSABLE_LABEL = "org.flowsteward.disposable"


def validate_owner(owner: str) -> str:
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", owner):
        raise ValueError("invalid owner identifier")
    return owner


@dataclass
class Resource:
    resource_id: str
    kind: str
    identity: str
    proof: dict[str, str]
    state: str = "active"
    disposable: bool = True
    consumers: list[str] = field(default_factory=list)
    retention: dict[str, str] = field(default_factory=dict)
    purpose: str = "task-private temporary resource"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    cleanup_trigger: str = "released after command exit, with no live leases or consumers"

    def __post_init__(self) -> None:
        if not isinstance(self.resource_id, str) or not re.fullmatch(
            r"[a-f0-9]{32}", self.resource_id
        ):
            raise ValueError("invalid resource ID")
        if self.kind not in KINDS or self.state not in STATES:
            raise ValueError("unknown resource kind or state")
        if not isinstance(self.identity, str) or not self.identity or self.identity.startswith("-"):
            raise ValueError("invalid resource identity")
        if any(
            not isinstance(value, str) or not value
            for value in (self.purpose, self.created_at, self.cleanup_trigger)
        ):
            raise ValueError("invalid resource lifecycle metadata")
        if type(self.disposable) is not bool:
            raise ValueError("invalid disposable flag")
        for mapping in (self.proof, self.retention):
            if not isinstance(mapping, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()
            ):
                raise ValueError("invalid resource metadata")
        if not isinstance(self.consumers, list) or any(
            not isinstance(v, str) or not v for v in self.consumers
        ):
            raise ValueError("invalid consumers")


@dataclass
class Ledger:
    owner: str
    token: str
    workspace: str
    created_at: str
    resources: list[Resource] = field(default_factory=list)
    run_pid: int | None = None
    run_pgid: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        validate_owner(self.owner)
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported ledger schema")
        if not re.fullmatch(r"[a-f0-9]{32}", self.token):
            raise ValueError("invalid ledger token")
        if self.run_pid is not None and (type(self.run_pid) is not int or self.run_pid <= 0):
            raise ValueError("invalid execution lease")
        if self.run_pgid is not None and (type(self.run_pgid) is not int or self.run_pgid <= 0):
            raise ValueError("invalid process-group lease")
        if not isinstance(self.workspace, str) or not isinstance(self.created_at, str):
            raise ValueError("invalid ledger paths or timestamps")
        if len({r.resource_id for r in self.resources}) != len(self.resources):
            raise ValueError("duplicate resource IDs")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Ledger:
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("unsupported ledger schema; legacy ledgers require manual review")
        try:
            fields = dict(data)
            fields["resources"] = [Resource(**r) for r in fields["resources"]]
            return cls(**fields)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("invalid ledger schema") from exc


@dataclass
class CleanupReceipt:
    owner: str
    apply: bool
    planned: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    blocked: dict[str, str] = field(default_factory=dict)
    retained: tuple[str, ...] = ()
    inventory_before: dict[str, list[str]] = field(default_factory=dict)
    inventory_after: dict[str, list[str]] = field(default_factory=dict)
    inventory_changes: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    free_bytes_before: int = 0
    free_bytes_after: int = 0
