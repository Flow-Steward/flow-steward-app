FROM docker:28-cli@sha256:625d9431a9f54c5a2bc90f24f0e1c3d55b1349fd857dd85035f98c2c9acbdd4d

ARG FS_BUILD_REVISION=unknown
LABEL org.opencontainers.image.revision="${FS_BUILD_REVISION}"

# AC17-R116: bash is here for scripts/release/build_candidate.sh, which the installer
# runs to produce the Scalable images and which declares `#!/usr/bin/env bash` and uses
# constructs POSIX sh does not have -- so pointing it at sh would fail differently and
# more quietly. Measured rather than assumed: of everything that script needs, only
# bash is absent from docker:28-cli; git, awk, cmp, mktemp and tar are all present.
#
# This does not contradict the POSIX shell requirement stated for install.sh: that is
# about the shell an operator must have on their own machine, and says nothing about
# what our own image may carry for a script of ours that asks for it.
#
# No git-lfs: the build's first act is a `git diff` over the mounted repository, and
# that needed the lfs filter only while Core shipped extension payloads in LFS. Core
# ships no extensions now and routes nothing through the filter, so git here never
# invokes it. tests/unit/test_install_script.py asks for git-lfs again the moment a
# pattern returns to .gitattributes.
#
# Candidate provenance reads the checkout's Git history and YAML ledger here.
# Product runtime images intentionally carry no Git, and the retained CLI carries
# no YAML dependency. Keep this build-only dependency in the installer runtime.
RUN apk add --no-cache bash openssl python3 py3-pip \
  && python3 -m venv /opt/flow-steward-installer \
  && /opt/flow-steward-installer/bin/pip install --no-cache-dir \
    boto3==1.38.0 \
    click==8.4.2 \
    cryptography==50.0.0 \
    pydantic==2.12.5 \
    PyYAML==6.0.3 \
    'psycopg[binary]==3.3.3' \
    rich==15.0.0 \
    textual==8.2.8 \
  && docker --version \
  && docker compose version \
  && python3 --version

WORKDIR /app

ENV HOME=/tmp \
    PATH=/opt/flow-steward-installer/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPYCACHEPREFIX=/tmp/flow-steward-pycache

ENTRYPOINT ["python3"]
