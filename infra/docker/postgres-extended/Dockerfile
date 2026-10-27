FROM pgvector/pgvector:0.8.5-pg17@sha256:d2ef61f42ef767baa5a1475393303cc235bcd92febd9d7014eddb48b41f3bad0

ARG AGE_REF=PG17/v1.6.0-rc0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        flex \
        git \
        bison \
        libreadline-dev \
        postgresql-server-dev-17 \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${AGE_REF}" https://github.com/apache/age.git /tmp/age \
    && make -C /tmp/age PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config install \
    && rm -rf /tmp/age
# Canonical first-install schema travels with the immutable PostgreSQL artifact;
# production installation does not require a host source checkout bind mount.
COPY db/schema/ /docker-entrypoint-initdb.d/
