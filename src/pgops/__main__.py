"""Entry point: argument parsing, server wiring, tool registration, stdio transport."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from fastmcp import FastMCP

from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.errors import PgopsError, tool_boundary
from pgops.tools.health import db_health
from pgops.tools.query import query_read
from pgops.tools.schema import Level, schema_inspect


def configure_logging(level: int = logging.INFO) -> None:
    """All logs go to stderr — never stdout.

    Under stdio transport, stdout IS the MCP protocol channel: the client parses it as
    a stream of JSON-RPC messages. A single stray log line (or `print()`) written there
    corrupts the stream and breaks the session in a way that looks like a client bug.
    logging.StreamHandler defaults to stderr, but it is set explicitly here because the
    consequence of getting it wrong is silent and confusing.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("pgops")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False


def build_server(config: PgopsConfig, conn_manager: ConnectionManager) -> FastMCP:
    mcp: FastMCP = FastMCP("pgops-mcp")

    # Tool names match docs/TOOLS.md exactly. FastMCP would otherwise derive the name
    # from the Python function, which is a private implementation detail — the tool
    # name is a public contract that agents and docs both depend on.
    @mcp.tool(name="schema.inspect")
    @tool_boundary
    async def schema_inspect_tool(
        level: Level = "summary", table: str | None = None
    ) -> dict[str, Any]:
        """Inspect database structure: tables, columns, indexes, constraints, sizes."""
        snapshot = await schema_inspect(conn_manager, level=level, table=table)
        return snapshot.to_dict(level)

    @mcp.tool(name="query.read")
    @tool_boundary
    async def query_read_tool(
        sql: str, limit: int | None = None, timeout_ms: int | None = None
    ) -> dict[str, Any]:
        """Execute a read-only statement (SELECT/WITH/EXPLAIN only)."""
        result = await query_read(conn_manager, config, sql, limit=limit, timeout_ms=timeout_ms)
        return result.to_dict()

    @mcp.tool(name="db.health")
    @tool_boundary
    async def db_health_tool() -> dict[str, Any]:
        """Health snapshot: connections, cache hit ratio, dead tuples, long-running
        queries, waiting locks — each finding with a severity and explanation."""
        report = await db_health(conn_manager)
        return report.to_dict()

    return mcp


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pgops-mcp")
    parser.add_argument("--dsn", default=None, help="Postgres DSN (overrides PGOPS_DSN)")
    parser.add_argument("--read-only", action="store_true", default=None, help="disable write tools")
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="connect, introspect, print a summary, then exit (no MCP transport)",
    )
    parser.add_argument("--verbose", action="store_true", help="debug-level logging on stderr")
    return parser.parse_args(argv)


async def _selfcheck(config: PgopsConfig) -> None:
    conn_manager = ConnectionManager(config)
    await conn_manager.start()
    try:
        health = await conn_manager.healthcheck()
        snapshot = await schema_inspect(conn_manager, level="summary")
        print(f"readonly pool: {'OK' if health['readonly'] else 'FAILED'}")
        print(f"tables in public schema: {len(snapshot.tables)}")
        for t in snapshot.tables:
            print(f"  - {t.name}: ~{t.estimated_rows} rows, {t.total_bytes} bytes")
    finally:
        await conn_manager.stop()


def main() -> None:
    args = parse_args()
    configure_logging(logging.DEBUG if args.verbose else logging.INFO)
    try:
        config = PgopsConfig.from_env(dsn=args.dsn, read_only=args.read_only)
    except PgopsError as exc:
        print(f"pgops-mcp: {exc.to_dict()}", file=sys.stderr)
        raise SystemExit(1) from exc

    if args.selfcheck:
        asyncio.run(_selfcheck(config))
        return

    conn_manager = ConnectionManager(config)

    async def _run() -> None:
        await conn_manager.start()
        try:
            mcp = build_server(config, conn_manager)
            await mcp.run_async(transport="stdio")
        finally:
            await conn_manager.stop()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
