# Date and timezone reproducibility

Workflow date operations use Python `zoneinfo.ZoneInfo`. Its normal lookup order
is the image's system timezone database through `zoneinfo.TZPATH`, followed by the
first-party Python `tzdata` package when the requested zone is absent from the
system paths. Installing the wheel does not override the system database. See
the [Python data-source contract](https://docs.python.org/3.12/library/zoneinfo.html#data-sources).
Flow Steward does not reset TZPATH or add a package-only resolver.

`requirements-runtime.txt` pins `tzdata==2026.6`; the generated runtime and Compact
locks include its distribution hashes. This is an availability fallback for
workflow execution in both deployment profiles. The release was verified against
[PyPI's versioned metadata](https://pypi.org/pypi/tzdata/2026.6/json); the verified
wheel declares IANA version `2026f`. The Python package version and the IANA
version identify different things. Neither proves the version of system data
that ZoneInfo actually selected.

## Release evidence

The Dockerfile pins the Python 3.12 slim base by digest. Record the final product
image digest as well: the effective system database belongs to that image's
filesystem and package set, and may differ from the fallback wheel. For each
workflow-executing image (worker/app runtime and Compact), release verification
records:

- the final immutable image digest and Python version;
- the actual TZPATH and first system file selected for `Europe/Berlin` and
  `Asia/Kathmandu`, or that each uses the package fallback;
- the system tzdb version where exposed by `tzdata.zi` or the distribution's
  package metadata, plus the installed fallback package/IANA versions;
- successful construction of both ZoneInfo keys in that built image.

Run the following read-only diagnostic **inside the built product image** with
its ordinary runtime configuration. A host result or an audit image result does
not establish the product image's database. This diagnostic does not change
ZoneInfo's lookup paths or environment.

```python
import platform
from importlib.metadata import version
from pathlib import Path
from zoneinfo import TZPATH, ZoneInfo

import tzdata

print("python:", platform.python_version())
print("TZPATH:", TZPATH)
print("fallback package:", version("tzdata"), "IANA:", tzdata.IANA_VERSION)
for key in ("Europe/Berlin", "Asia/Kathmandu"):
    zone = ZoneInfo(key)
    source = next((Path(root) / key for root in TZPATH if (Path(root) / key).is_file()), None)
    if source is None:
        print(key, "loaded:", zone.key, "source: tzdata wheel", tzdata.IANA_VERSION)
        continue
    print(key, "loaded:", zone.key, "system file:", source)
    root = next(Path(root) for root in TZPATH if Path(root) / key == source)
    metadata = root / "tzdata.zi"
    if metadata.is_file():
        with metadata.open() as stream:
            print("system tzdb header:", stream.readline().strip())
    else:
        print("system tzdb version: unavailable from tzdata.zi; record OS package metadata")
```

If system version metadata is unavailable, record that limitation instead of
substituting the wheel version. Mixed system/fallback lookup is possible for
different keys, so capture each zone's source. Keep this evidence with the final
image digest and date contract test results. Updating timezone data requires
fresh verification of date/DST vectors against the resulting image.

## Updating the fallback

Verify a release and its artifacts on official PyPI, change only the explicit
runtime pin, and regenerate the existing locks with Python 3.12 using their
canonical header commands:

```sh
pip-compile --allow-unsafe --generate-hashes --output-file=requirements-runtime.lock requirements-runtime.txt
pip-compile --allow-unsafe --generate-hashes --output-file=requirements-compact.lock requirements-control.txt requirements-mcp.txt requirements-runtime.txt
```

Retain existing unrelated pins and review the generated diff. The runtime and
Compact images install these locks with `--require-hashes`. Include the new
dependency in the ordinary pip-audit gate and record the effective image data
again after packaging. A package pin alone is not proof of system tzdb
reproducibility.
