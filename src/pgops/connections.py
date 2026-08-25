"""ConnectionManager: two asyncpg pools per DSN (ADR summary in ARCHITECTURE.md).

readonly pool:
    - Eager: created at startup so `db.health` / `schema.inspect` work immediately.
    - Every connection runs `SET default_transaction_read_only = on` on acquire
      (see `_init_readonly_connection`). This is the actual enforcement mechanism:
      it works even when PGOPS_DSN points at a superuser role, because Postgres
      rejects writes at the executor level for any transaction in read-only mode —
      independent of GRANTs. If the operator *also* points PGOPS_DSN (or the optional
      PGOPS_READONLY_DSN) at a least-privilege role, that's a second, belt-and-suspenders
      layer, but it is not required for the guarantee to hold.
    - Why not rely on GRANTs alone: we don't control how the user provisioned their
      DSN's role, and requiring "first go create a readonly role" would break the
      <2 min install goal (G5). Session-level read-only mode is a guarantee we can give
      unconditionally from connection code alone.

readwrite pool:
    - Lazy: not created until the first `query.write`/`migration.apply` call (Phase 2+),
      so a pure read-only session (or `--read-only` mode) never opens a write-capable
      connection at all. Smaller max size by default — writes are expected to be
      infrequent and somewhat serialized relative to reads (see ARCHITECTURE.md
      "two clients share one server" failure mode).

Statement timeouts are applied per-call via `SET LOCAL statement_timeout` inside an
explicit transaction, not as a pool-wide session default — `LOCAL` scoping means the
setting reverts at transaction end, so one call's timeout tier can never leak onto the
next caller that happens to reuse the same pooled connection.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from pgops.config import PgopsConfig
from pgops.errors import ErrorCode, PgopsError
from pgops.observability import record_pool_timeout, set_db_up

logger = logging.getLogger("pgops.connections")


async def _init_readonly_connection(conn: asyncpg.Connection) -> None:
    await conn.execute("SET default_transaction_read_only = on")


class ConnectionManager:
    def __init__(self, config: PgopsConfig) -> None:
        self._config = config
        self._readonly_pool: asyncpg.Pool | None = None
        self._readwrite_pool: asyncpg.Pool | None = None

    async def start(self) -> None:
        try:
            self._readonly_pool = await asyncpg.create_pool(
                dsn=self._config.readonly_dsn or self._config.dsn,
                min_size=self._config.pools.readonly_min,
                max_size=self._config.pools.readonly_max,
                setup=_init_readonly_connection,
            )
        except (OSError, asyncpg.PostgresError) as exc:
            raise PgopsError(
                ErrorCode.CONNECTION_FAILED,
                f"could not connect readonly pool: {exc}",
                hint="check PGOPS_DSN / PGOPS_READONLY_DSN and that Postgres is reachable",
            ) from exc

    async def stop(self) -> None:
        if self._readonly_pool is not None:
            await self._readonly_pool.close()
            self._readonly_pool = None
        if self._readwrite_pool is not None:
            await self._readwrite_pool.close()
            self._readwrite_pool = None

    @property
    def readonly_pool(self) -> asyncpg.Pool:
        if self._readonly_pool is None:
            raise PgopsError(
                ErrorCode.INTERNAL_ERROR,
                "readonly pool used before start()",
            )
        return self._readonly_pool

    @asynccontextmanager
    async def acquire_readonly(self) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a readonly connection with a bounded wait.

        Every tool goes through here rather than calling `pool.acquire()` directly, so
        pool exhaustion surfaces as a structured POOL_EXHAUSTED error instead of an
        indefinite hang.
        """
        timeout = self._config.pools.acquire_timeout_s
        try:
            conn = await asyncio.wait_for(self.readonly_pool.acquire(), timeout=timeout)
        except TimeoutError as exc:
            logger.warning("readonly pool exhausted after %.1fs wait", timeout)
            record_pool_timeout()
            raise PgopsError(
                ErrorCode.POOL_EXHAUSTED,
                f"no readonly connection available within {timeout:.1f}s",
                hint="a previous query may still be running; retry, or raise "
                "PGOPS_READONLY_POOL_MAX",
            ) from exc
        try:
            yield conn
        finally:
            await self.readonly_pool.release(conn)

    async def readwrite_pool(self) -> asyncpg.Pool:
        if self._config.read_only:
            raise PgopsError(
                ErrorCode.READ_ONLY_MODE,
                "server started with --read-only; write tools are disabled",
            )
        if self._readwrite_pool is None:
            try:
                self._readwrite_pool = await asyncpg.create_pool(
                    dsn=self._config.dsn,
                    min_size=0,
                    max_size=self._config.pools.readwrite_max,
                )
            except (OSError, asyncpg.PostgresError) as exc:
                raise PgopsError(
                    ErrorCode.CONNECTION_FAILED,
                    f"could not open readwrite pool: {exc}",
                ) from exc
        return self._readwrite_pool

    async def healthcheck(self) -> dict[str, bool]:
        result = {"readonly": False, "readwrite": False}
        try:
            async with self.readonly_pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            result["readonly"] = True
        except (OSError, asyncpg.PostgresError):
            pass
        set_db_up(result["readonly"])
        if self._readwrite_pool is not None:
            try:
                async with self._readwrite_pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
                result["readwrite"] = True
            except (OSError, asyncpg.PostgresError):
                pass
        return result
