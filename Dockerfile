# syntax=docker/dockerfile:1
#
# Multi-stage build for garmin-coach-mcp.
#
#   builder  installs the package (and any wheels that need a compiler) into /opt/venv
#   runtime  copies that venv onto a clean slim image — no compiler, no pip cache,
#            no build metadata, and it runs as a non-root user.
#
#   docker build -t garmin-coach-mcp --build-arg VERSION=0.1.0 .

ARG PYTHON_VERSION=3.12

# --------------------------------------------------------------------------- #
# Stage 1 — build
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# Only needed if a dependency has no manylinux wheel for this platform.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /src
# pyproject declares readme = README.md and license-files = LICENSE, so both must
# be present for the build to succeed.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# --------------------------------------------------------------------------- #
# Stage 2 — runtime
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS runtime

# Re-declared: an ARG set before the first FROM is not visible inside a stage.
ARG PYTHON_VERSION
ARG VERSION=0.1.0

LABEL org.opencontainers.image.title="garmin-coach-mcp" \
      org.opencontainers.image.description="Model Context Protocol server for Garmin Connect: activities, health metrics and structured training plans." \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.source="https://github.com/mrosano1987/garmin-mcp-server-1" \
      org.opencontainers.image.url="https://github.com/mrosano1987/garmin-mcp-server-1" \
      org.opencontainers.image.documentation="https://github.com/mrosano1987/garmin-mcp-server-1#readme" \
      org.opencontainers.image.base.name="docker.io/library/python:${PYTHON_VERSION}-slim"

# Container defaults. Every one of them can be overridden with `-e` /
# `environment:`, or by appending the matching CLI flag after the image name.
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    GARMIN_TRANSPORT=streamable-http \
    GARMIN_HOST=0.0.0.0 \
    GARMIN_PORT=8000 \
    GARMIN_TOKEN_DIR=/data/tokens \
    GARMIN_LOG_LEVEL=INFO

COPY --from=builder /opt/venv /opt/venv

RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin garmin \
    && mkdir -p /data/tokens \
    && chown -R garmin:garmin /data

USER garmin
WORKDIR /home/garmin

# Cached Garmin OAuth tokens live here — mount a named volume so the first
# login survives container recreation and credentials can then be removed.
VOLUME ["/data/tokens"]

EXPOSE 8000

# Speaks real MCP to the streamable-http endpoint: opens a session with an
# `initialize` request and closes it again with DELETE, so repeated probes do
# not leak sessions. Only meaningful for the http/sse transports — if you run
# the image with `--transport stdio`, start it with `--no-healthcheck`.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c 'import json,os,urllib.request as u; \
o=u.build_opener(u.ProxyHandler({})); \
a="http://127.0.0.1:"+os.environ.get("GARMIN_PORT","8000")+"/mcp"; \
b=json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"healthcheck","version":"1"}}}).encode(); \
r=o.open(u.Request(a,data=b,headers={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}),timeout=5); \
s=r.headers.get("mcp-session-id"); r.close(); \
s and o.open(u.Request(a,method="DELETE",headers={"mcp-session-id":s}),timeout=5)' \
    || exit 1

# No shell wrapper and no startup banner wall: the CLI does the diagnostics.
# `docker run --rm garmin-coach-mcp --check` runs the doctor and exits.
ENTRYPOINT ["garmin-coach-mcp"]
CMD []
