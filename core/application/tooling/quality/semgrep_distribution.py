"""Build an explicit downstream Semgrep wheel without changing upstream code."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

UPSTREAM_VERSION = "1.183.0"
LOCAL_VERSION = "1.183.0+fs.1"
# Official PyPI Linux artifacts, checked before any archive is parsed.
UPSTREAM_DIGESTS = frozenset(
    {
        "b058cc62ca32af79d2eaf44c3ff0931bfecc74857107eaf6b1b0ebb6676604ec",
        "f36115c4d0cb43056ceb308273421a2f6a35e055e7e029601e130850f836646e",
    }
)
DEPENDENCY_PATCH = {
    "mcp==1.29.0": "mcp==1.30.0",
    "opentelemetry-api~=1.37.0": "opentelemetry-api==1.45.1",
    "opentelemetry-sdk~=1.37.0": "opentelemetry-sdk==1.45.1",
    "opentelemetry-exporter-otlp-proto-http~=1.37.0": "opentelemetry-exporter-otlp-proto-http==1.45.1",
    "opentelemetry-instrumentation-requests~=0.58b0": "opentelemetry-instrumentation-requests==0.66b1",
    "opentelemetry-instrumentation-threading~=0.58b0": "opentelemetry-instrumentation-threading==0.66b1",
}


def _safe_member(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or not name:
        raise ValueError("unsafe wheel member")


def _installed_name(name: str) -> str:
    prefix = f"semgrep-{LOCAL_VERSION}.data/"
    if name.startswith(prefix):
        bucket, separator, relative = name[len(prefix) :].partition("/")
        if bucket not in {"purelib", "platlib"} or not separator:
            raise ValueError("unsupported wheel installation bucket")
        return relative
    return name


def rebuild_wheel(source: Path, output: Path, *, expected_sha: str) -> tuple[Path, dict[str, Any]]:
    """Verify a trusted artifact digest and change only packaging metadata."""
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("upstream wheel digest mismatch")
    old = f"semgrep-{UPSTREAM_VERSION}"
    new = f"semgrep-{LOCAL_VERSION}"
    members: dict[str, tuple[zipfile.ZipInfo, bytes]] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for info in archive.infolist():
            _safe_member(info.filename)
            if info.filename in members or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("duplicate or symlink wheel member")
            members[info.filename] = (info, archive.read(info.filename))
    metadata_name = old + ".dist-info/METADATA"
    metadata = members[metadata_name][1].decode("utf-8")
    if (
        metadata.count("Name: semgrep\n") != 1
        or metadata.count(f"Version: {UPSTREAM_VERSION}\n") != 1
    ):
        raise ValueError("unexpected upstream distribution identity")
    metadata = metadata.replace(f"Version: {UPSTREAM_VERSION}\n", f"Version: {LOCAL_VERSION}\n")
    for before, after in DEPENDENCY_PATCH.items():
        line = f"Requires-Dist: {before}\n"
        if metadata.count(line) != 1:
            raise ValueError(f"unexpected upstream dependency: {before}")
        metadata = metadata.replace(line, f"Requires-Dist: {after}\n")
    if not source.name.startswith(old + "-") or not source.name.endswith(".whl"):
        raise ValueError("unexpected wheel filename")
    output.mkdir(parents=True, exist_ok=True)
    target = output / source.name.replace(old + "-", new + "-", 1)
    records: list[tuple[str, str, int | str]] = []
    installed: dict[str, str] = {}
    with zipfile.ZipFile(target, "w") as archive:
        for name, (info, data) in members.items():
            if name == old + ".dist-info/RECORD":
                continue
            if name == metadata_name:
                data = metadata.encode("utf-8")
            if name.startswith((old + ".dist-info/", old + ".data/")):
                name = new + name[len(old) :]
            info.filename = name
            archive.writestr(info, data)
            digest = hashlib.sha256(data).digest()
            records.append(
                (
                    name,
                    "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode(),
                    len(data),
                )
            )
            if not name.endswith("/"):
                relative = _installed_name(name)
                if relative in installed:
                    raise ValueError("duplicate installed wheel path")
                installed[relative] = digest.hex()
        record_name = new + ".dist-info/RECORD"
        records.append((record_name, "", ""))
        record = io.StringIO()
        csv.writer(record, lineterminator="\n").writerows(records)
        archive.writestr(record_name, record.getvalue())
    return target, {
        "source_sha256": expected_sha,
        "upstream_version": UPSTREAM_VERSION,
        "local_version": LOCAL_VERSION,
        "files": installed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_directory", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    sources = list(args.source_directory.glob("semgrep-*.whl"))
    if len(sources) != 1:
        raise ValueError("exactly one official wheel is required")
    source = sources[0]
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest not in UPSTREAM_DIGESTS:
        raise ValueError("unrecognized official wheel digest")
    _, receipt = rebuild_wheel(source, args.output_directory, expected_sha=digest)
    args.receipt.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
