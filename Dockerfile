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

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request; r=urllib.request.Request('http://localhost:8000/mcp',method='POST',headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream'},data=b'{\"jsonrpc\":\"2.0\",\"method\":\"ping\",\"id\":1}'); urllib.request.urlopen(r)" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
