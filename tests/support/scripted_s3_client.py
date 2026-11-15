"""A scripted S3 client for executing generated storage probe scripts in tests."""

from __future__ import annotations

from collections.abc import Iterable

from botocore.exceptions import ClientError


def client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class ScriptedS3Client:
    """Answers bucket calls from a script: ``None`` means success, a string is an error code."""

    def __init__(
        self,
        *,
        heads: Iterable[str | None],
        create: str | None = None,
    ) -> None:
        self._heads = list(heads)
        self._create = create
        self._objects: dict[str, bytes] = {}
        self.calls: list[str] = []

    def head_bucket(self, **_kwargs: object) -> None:
        self.calls.append("head")
        if not self._heads:
            raise AssertionError("the probe checked the bucket more often than scripted")
        outcome = self._heads.pop(0)
        if outcome is not None:
            raise client_error(outcome, "HeadBucket")

    def create_bucket(self, **_kwargs: object) -> None:
        self.calls.append("create")
        if self._create is not None:
            raise client_error(self._create, "CreateBucket")

    def put_object(self, *, Key: str, Body: bytes = b"", **_kwargs: object) -> None:
        self.calls.append("put")
        self._objects[Key] = bytes(Body)

    def get_object(self, *, Key: str, **_kwargs: object) -> dict[str, object]:
        self.calls.append("get")
        return {"Body": _Body(self._objects[Key])}

    def delete_object(self, *, Key: str, **_kwargs: object) -> None:
        self.calls.append("delete")
        self._objects.pop(Key, None)


class _Body:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        return None
