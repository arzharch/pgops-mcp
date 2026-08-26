# Container distribution for the MCP Registry's `oci` package type, and for anyone who
# would rather not put a Python toolchain on the machine that talks to their database.
#
# Two-stage so the runtime image carries no build backend and no compiler: the wheel is
# built once, installed into a virtualenv, and only that venv is copied forward.

FROM python:3.12-slim AS build

# uv is the build backend this project declares; installing it here keeps the runtime
# image free of it.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

# --no-dev: the test stack (pytest, testcontainers, hypothesis) has no business in a
# runtime image, and testcontainers in particular would drag the Docker SDK in for the
# wrong reason.
RUN uv build --wheel --out-dir /wheels \
 && uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache /wheels/*.whl


FROM python:3.12-slim AS runtime

# The image ships as an MCP server; this annotation is what the MCP Registry checks to
# verify that whoever pushed the image also owns the server name in server.json.
LABEL io.modelcontextprotocol.server.name="io.github.arzharch/pgops-mcp"
LABEL org.opencontainers.image.source="https://github.com/arzharch/pgops-mcp"
LABEL org.opencontainers.image.description="Safe, audited PostgreSQL operations for AI agents"
LABEL org.opencontainers.image.licenses="MIT"

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Non-root by default. The server needs no privileges of its own — it holds a database
# DSN, and the env.* tools reach Docker through a socket that has to be mounted
# deliberately. Running as root would only widen what a compromise reaches.
RUN useradd --create-home --uid 10001 pgops \
 && mkdir -p /var/lib/pgops \
 && chown -R pgops:pgops /var/lib/pgops
USER pgops

# The audit log must outlive the container or it is not an audit log. Declaring the
# volume makes that explicit rather than leaving it to be discovered after an incident.
ENV PGOPS_AUDIT_LOG=/var/lib/pgops/audit.jsonl
VOLUME ["/var/lib/pgops"]

# stdio is the default transport and needs no port. HTTP is opt-in and refuses to start
# without a public key, so nothing is exposed here by default:
#   docker run ... pgops-mcp --transport http --host 0.0.0.0 --public-key /keys/pub.pem
ENTRYPOINT ["pgops-mcp"]
