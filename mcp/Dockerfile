# The MCP stdio server, and only that. The chat service has its own image in
# docker/Dockerfile — this one serves no HTTP, opens no port and owns no data directory.
#
# Pinned by digest for the same reason the service image is: `3.12-slim` moves.
FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

# Unbuffered because the transport *is* stdout: a buffered frame is a reply the client
# never sees until the next one flushes it.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    TECHNOCORE_URL=https://technocore.chat

# From PyPI rather than the source beside it, so the image runs the same artifact
# `uvx technocore-mcp` does. The package declares no dependencies — the service it wraps
# needs no client library — so this resolves exactly one wheel.
RUN pip install --no-cache-dir technocore-mcp==0.1.0 \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin mcp

USER 10001

# stdio: the process holds stdin open and speaks JSON-RPC over it. Sitting idle with no
# output is the correct behaviour, not a hang.
CMD ["technocore-mcp"]
