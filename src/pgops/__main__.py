"""Entry point: argument parsing, server wiring, tool registration, stdio transport."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from pgops.audit import AuditLog
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.errors import PgopsError, tool_boundary
from pgops.guardrails import ConfirmationTokenStore
from pgops.tools.advisor import index_advise
from pgops.tools.explain import query_explain
from pgops.tools.health import db_health
from pgops.tools.migrations import migration_apply, migration_history, migration_plan
from pgops.tools.query import query_read
from pgops.tools.schema import Level, schema_inspect
from pgops.tools.write import query_write


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
    audit = AuditLog(config.audit_path)
    tokens = ConfirmationTokenStore(ttl_s=config.confirm_token_ttl_s)

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

    if not config.read_only:

        @mcp.tool(name="query.write")
        @tool_boundary
        async def query_write_tool(
            sql: str, confirm_token: str | None = None, timeout_ms: int | None = None
        ) -> dict[str, Any]:
            """Execute a mutating statement (INSERT/UPDATE/DELETE/DDL).

            Destructive statements and unbounded UPDATE/DELETE are refused on the first
            call and return a confirmation token; call again with that token to execute.
            Relay the refusal reason to the user before doing so.
            """
            result = await query_write(
                conn_manager,
                config,
                audit,
                tokens,
                sql,
                confirm_token=confirm_token,
                timeout_ms=timeout_ms,
            )
            return result.to_dict()

    @mcp.tool(name="query.explain")
    @tool_boundary
    async def query_explain_tool(
        sql: str,
        analyze: bool = False,
        confirm_token: str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Explain a statement and return the plan plus actionable verdicts.

        analyze=false (default) plans without executing — always safe. analyze=true
        executes the statement to collect real timings; for a mutating statement that
        runs inside a transaction which is always rolled back, and requires the same
        confirmation token as query.write.
        """
        result = await query_explain(
            conn_manager,
            config,
            audit,
            tokens,
            sql,
            analyze=analyze,
            confirm_token=confirm_token,
            timeout_ms=timeout_ms,
        )
        return result.to_dict()

    @mcp.tool(name="index.advise")
    @tool_boundary
    async def index_advise_tool(limit: int = 10) -> dict[str, Any]:
        """Index recommendations: unused indexes, redundant indexes, sequential-scan
        hotspots, and the slowest statements by total time."""
        advice = await index_advise(conn_manager, limit=limit)
        return advice.to_dict()

    @mcp.tool(name="migration.plan")
    @tool_boundary
    async def migration_plan_tool(
        target: dict[str, Any], allow_drops: bool = False, dry_run: bool = True
    ) -> dict[str, Any]:
        """Plan a migration from a target schema. Executes nothing.

        `target` describes the desired state, e.g.
        {"tables": {"orders": {"columns": {"note": {"type": "text"}}}}}.
        Returns ordered steps, each annotated with lock impact, an estimated duration
        with confidence, and a safer alternative where one exists. Tables or columns
        absent from the target are left alone unless allow_drops=true.
        """
        plan = await migration_plan(
            conn_manager, config, target, allow_drops=allow_drops, dry_run=dry_run
        )
        return plan.to_dict()

    if not config.read_only:

        @mcp.tool(name="migration.apply")
        @tool_boundary
        async def migration_apply_tool(
            plan_id: str, confirm_token: str | None = None, name: str = "unnamed"
        ) -> dict[str, Any]:
            """Apply a plan produced by migration.plan.

            Destructive or high-risk plans are refused on the first call and return a
            confirmation token. Records the migration in the pgops_migrations ledger.
            """
            return await migration_apply(
                conn_manager, config, audit, tokens, plan_id,
                confirm_token=confirm_token, name=name,
            )

    @mcp.tool(name="migration.history")
    @tool_boundary
    async def migration_history_tool(limit: int = 20) -> dict[str, Any]:
        """Applied-migration history from the ledger, including any interrupted
        (in_flight) migration that needs manual resolution."""
        return await migration_history(conn_manager, limit=limit)

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
        "--audit-log", default=None, help="path to the JSONL audit log (default ~/.pgops/audit.jsonl)"
    )
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
        config = PgopsConfig.from_env(
            dsn=args.dsn,
            read_only=args.read_only,
            audit_path=Path(args.audit_log) if args.audit_log else None,
        )
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
