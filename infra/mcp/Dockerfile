FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash fs \
    && install -d -o fs -g fs /app/logs

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1


FROM base AS runtime

COPY --chmod=u=rwX,go=rX infra/docker/sitecustomize.py /usr/local/lib/python3.12/site-packages/sitecustomize.py
COPY --chmod=u=rwX,go=rX requirements-mcp.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements-mcp.lock

COPY --chmod=u=rwX,go=rX --chown=fs:fs core/__init__.py core/__init__.py
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/infrastructure/__init__.py core/infrastructure/__init__.py
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/infrastructure/mcp_dev_proxy.py core/infrastructure/mcp_dev_proxy.py
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/infrastructure/runtime_env.py core/infrastructure/runtime_env.py
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/entrypoints/mcp/ core/entrypoints/mcp/

ARG FS_BUILD_REVISION=unknown
LABEL org.opencontainers.image.revision="${FS_BUILD_REVISION}"
ENV FS_MCP_IMAGE_REVISION=${FS_BUILD_REVISION}

EXPOSE 8100

USER fs

CMD ["python", "-m", "core.entrypoints.mcp.server", "--transport", "streamable-http", "--port", "8100"]


FROM runtime AS local-runtime
