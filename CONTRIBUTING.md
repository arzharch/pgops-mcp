# Contributing

This is the path for working **on** pgops-mcp. To just *use* it, see
[SETUP.md](SETUP.md) — you do not need a checkout.

## Source checkout

```bash
git clone https://github.com/arzharch/pgops-mcp
cd pgops-mcp
uv sync --extra dev
```

`uv sync --extra dev` installs the test stack (pytest, testcontainers, hypothesis).
Those are deliberately *not* runtime dependencies — a user installing the server should
never receive a testing library.

## Gates

All three must pass before a commit:

```bash
uv run pytest -q      # 436 tests; needs a running Docker daemon
uv run ruff check .
uv run mypy src
```

The suite needs Docker because the guardrail tests run against a real PostgreSQL 16
container rather than mocks. That is a deliberate architectural decision, not an
oversight — see [internal/adr/](internal/adr/) (ADR-005). A guardrail that is only ever
proven against a fake has been proven against the wrong thing: the interesting failures
(`default_transaction_read_only`, lock escalation, transactional DDL, `relfilenode`
changes on rewrite) are behaviours of the real database.

Markers:

- `-m "not slow"` skips the suites that spawn the server as a subprocess.
- `-m live` runs the benchmark and evaluation suites; they are excluded by default.

## Dev database

A seeded stack with a 1.2M-row `orders` table, on host port **5435** so it does not
collide with a local Postgres on 5432:

```bash
docker compose up -d
export PGOPS_DSN="postgresql://pgops:pgops_dev@localhost:5435/pgops_demo"
uv run pgops-mcp --selfcheck
```

## Style

Docstrings carry the *reasoning* — why this approach and not the obvious alternative,
and what breaks under the alternative. A comment restating what the line does is noise;
a comment recording the failure that motivated the line is the point. Several of the
longer module docstrings exist because a subtle bug was found there, and the next reader
needs to know it was found rather than rediscover it.

## Releasing

Publishing is version-gated and runs from CI — see
[.github/workflows/publish.yml](.github/workflows/publish.yml). The sequence matters:

1. Bump `version` in `pyproject.toml`, `src/pgops/__init__.py`, and **both** the top
   level and the `packages[]` entries of `server.json`. They must agree; the registry
   rejects a mismatch.
2. Tag `vX.Y.Z` and push. CI builds, publishes to PyPI via Trusted Publishing, pushes
   the container image to GHCR, and only then publishes `server.json` to the MCP
   Registry.

The order is not arbitrary: the MCP Registry hosts **metadata only**. It verifies that
the PyPI package really exists and that its README contains the
`mcp-name: io.github.arzharch/pgops-mcp` marker, so publishing metadata before the
artifact fails validation.
