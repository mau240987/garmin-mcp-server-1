FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 app

COPY entrypoint.sh .
RUN chmod +x entrypoint.sh && mkdir -p /data/tokens && chown app /data/tokens

# Garmin tokens persisted here (mount as volume)
ENV GARMIN_TOKEN_DIR=/data/tokens
USER app

EXPOSE 8000

# Liveness only: a JSON-RPC ping without an MCP session is rejected with HTTP 400.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8000), 3).close()" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
