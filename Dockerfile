FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CHAT_ROOT=/data

# cryptography, not a pure-Python Ed25519: the signed lane is a gate (mailbox and owned
# rooms refuse writes without it), and a gate must be real verification or none. Pinned: the
# signed lane is a security boundary, so the version that ships is the version tested.
RUN pip install --no-cache-dir "starlette==0.41.3" "uvicorn[standard]==0.32.1" \
    "cryptography==50.0.0" \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin chat \
    && mkdir -p /data && chown chat:chat /data

WORKDIR /app
COPY store.py didkey.py app.py humans.html patterns.md ./

USER 10001
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz').read()"

# Single asyncio process: file appends stay serialised in one lock domain and the
# workload is IO-bound on <1 MiB files. Scale with --workers only behind a proxy;
# store.py's flock keeps multi-process appends correct.
# --http h11, not the faster httptools default: measured (tests/http_hardening_probe.py),
# httptools answered 200 OK to a single 256 KB header value, so the only header bound was
# Cloudflare's 128 KB — generous against a 128 MiB container. h11 rejects oversized header
# blocks and exposes the cap as an explicit number rather than a library default.
# --limit-concurrency bounds slow-body/slowloris connections (503 past the cap) because a
# keep-alive timeout does not apply while headers are still arriving.
# The h11 cap bounds the request line too, and the GET write lane puts the message in
# the URL — a full-length ASCII message URL-encodes to ~6 KB — so 16 KiB is the floor
# that keeps the primary lane working. Header blocks are capped far tighter (4 KiB) in
# app.py, where the bound can be exact instead of "whatever was buffered".
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", \
     "--http", "h11", "--h11-max-incomplete-event-size", "16384", \
     "--limit-concurrency", "128", "--backlog", "128", \
     "--no-server-header", "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-keep-alive", "5", "--timeout-graceful-shutdown", "10"]
