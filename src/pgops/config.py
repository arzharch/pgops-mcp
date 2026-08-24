"""Server configuration: DSNs, timeout tiers, pool sizing, safety flags.

Every value has a PGOPS_* env var and, in __main__.py, a CLI override — env vars are
what a Claude Desktop / Cursor mcpServers.json entry sets; CLI flags are for manual
`uv run pgops-mcp ...` usage. CLI wins when both are given.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from pgops.errors import ErrorCode, PgopsError


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            f"{name}={raw!r} is not a valid integer",
        ) from None


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class TimeoutTiers:
    """statement_timeout in ms. `default` applies unless a tool asks for a higher
    tier explicitly; `max` is a hard ceiling no tool call — however configured — can
    exceed. Keeps one runaway EXPLAIN ANALYZE from parking a pool connection forever."""

    default_ms: int = field(default_factory=lambda: _int_env("PGOPS_DEFAULT_TIMEOUT_MS", 5_000))
    max_ms: int = field(default_factory=lambda: _int_env("PGOPS_MAX_TIMEOUT_MS", 30_000))

    def resolve(self, requested_ms: int | None) -> int:
        if requested_ms is None:
            return self.default_ms
        if requested_ms <= 0:
            raise PgopsError(
                ErrorCode.INVALID_ARGUMENT,
                "timeout_ms must be positive",
            )
        return min(requested_ms, self.max_ms)


@dataclass(slots=True)
class RowLimits:
    default: int = field(default_factory=lambda: _int_env("PGOPS_DEFAULT_ROW_LIMIT", 100))
    max: int = field(default_factory=lambda: _int_env("PGOPS_MAX_ROW_LIMIT", 10_000))

    def resolve(self, requested: int | None) -> int:
        if requested is None:
            return self.default
        if requested <= 0:
            raise PgopsError(ErrorCode.INVALID_ARGUMENT, "limit must be positive")
        if requested > self.max:
            raise PgopsError(
                ErrorCode.ROW_LIMIT_EXCEEDED,
                f"requested limit {requested} exceeds server max {self.max}",
                hint=f"lower limit to <= {self.max}",
            )
        return requested


@dataclass(slots=True)
class PoolSizing:
    # Local, single-agent usage rarely needs more than a handful of connections;
    # keep both pools small by default and let production deployments raise them
    # relative to Postgres max_connections and expected concurrent MCP clients.
    readonly_min: int = field(default_factory=lambda: _int_env("PGOPS_READONLY_POOL_MIN", 1))
    readonly_max: int = field(default_factory=lambda: _int_env("PGOPS_READONLY_POOL_MAX", 5))
    readwrite_max: int = field(default_factory=lambda: _int_env("PGOPS_READWRITE_POOL_MAX", 2))
    # Ceiling on how long a tool call waits for a free pooled connection. Without it,
    # asyncpg's acquire() waits forever: if every connection is held by slow queries,
    # new tool calls hang with no error and no timeout — the agent just stops getting
    # responses, which is far worse to debug than a clear "pool exhausted".
    acquire_timeout_s: float = field(
        default_factory=lambda: _int_env("PGOPS_POOL_ACQUIRE_TIMEOUT_MS", 10_000) / 1000
    )


def _default_audit_path() -> Path:
    raw = os.environ.get("PGOPS_AUDIT_LOG")
    if raw:
        return Path(raw).expanduser()
    # Under the home directory, not the CWD: an MCP server is launched by the client
    # (Claude Desktop, Cursor) with a working directory the user never chose and may
    # not be able to find again. The audit trail has to live somewhere predictable.
    return Path.home() / ".pgops" / "audit.jsonl"


@dataclass(slots=True)
class PgopsConfig:
    dsn: str
    readonly_dsn: str | None = None
    read_only: bool = False
    approval_mode: bool = False
    audit_path: Path = field(default_factory=_default_audit_path)
    confirm_token_ttl_s: int = field(
        default_factory=lambda: _int_env("PGOPS_CONFIRM_TOKEN_TTL_S", 300)
    )
    timeouts: TimeoutTiers = field(default_factory=TimeoutTiers)
    row_limits: RowLimits = field(default_factory=RowLimits)
    pools: PoolSizing = field(default_factory=PoolSizing)

    @classmethod
    def from_env(
        cls,
        *,
        dsn: str | None = None,
        read_only: bool | None = None,
        audit_path: Path | None = None,
    ) -> PgopsConfig:
        resolved_dsn = dsn or os.environ.get("PGOPS_DSN")
        if not resolved_dsn:
            raise PgopsError(
                ErrorCode.DSN_MISSING,
                "no Postgres DSN provided",
                hint="set PGOPS_DSN or pass --dsn",
            )
        kwargs: dict[str, object] = {}
        if audit_path is not None:
            kwargs["audit_path"] = audit_path
        return cls(
            dsn=resolved_dsn,
            readonly_dsn=os.environ.get("PGOPS_READONLY_DSN"),
            read_only=read_only if read_only is not None else _bool_env("PGOPS_READ_ONLY", False),
            approval_mode=_bool_env("PGOPS_APPROVAL_MODE", False),
            **kwargs,  # type: ignore[arg-type]
        )
