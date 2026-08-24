"""Entry point: argument parsing, server wiring, tool registration, stdio transport."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP

from pgops.audit import AuditLog
from pgops.auth import (
    Scope,
    build_verifier,
    describe_scopes,
    generate_keypair,
    issue_token,
    load_public_key,
)
from pgops.completions import register_completions
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.errors import PgopsError, tool_boundary
from pgops.guardrails import ConfirmationTokenStore
from pgops.middleware import ScopeEnforcement
from pgops.prompts import (
    diagnose_slow_query,
    explain_safety_model,
    incident_triage,
    plan_safe_migration,
    review_index_health,
)
from pgops.resources import (
    audit_resource,
    config_resource,
    health_resource,
    migrations_resource,
    schema_resource,
    schema_summary_resource,
    table_resource,
)
from pgops.tools.advisor import index_advise
from pgops.tools.environment import (
    container_exec,
    container_logs,
    container_restart,
    container_stats,
    env_correlate,
    env_topology,
)
from pgops.tools.explain import query_explain
from pgops.tools.health import db_health
from pgops.tools.migrations import (
    migration_apply,
    migration_describe,
    migration_history,
    migration_plan,
)
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


def build_server(config: PgopsConfig, conn_manager: ConnectionManager, auth: Any = None) -> FastMCP:
    mcp: FastMCP = FastMCP("pgops-mcp", auth=auth)

    # The token verifier answers "is this caller real". It does NOT answer "may this
    # caller run this tool" — its required_scopes list is checked once per request
    # against a single server-wide value, so without this middleware a pgops:read token
    # can call query.write. Only added under auth: over stdio there is no token to
    # check and every call would be denied for lacking a scope nobody issued.
    if auth is not None:
        mcp.add_middleware(ScopeEnforcement())

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
            sql: str,
            confirm_token: str | None = None,
            timeout_ms: int | None = None,
            ctx: Context | None = None,
        ) -> dict[str, Any]:
            """Execute a mutating statement (INSERT/UPDATE/DELETE/DDL).

            Destructive statements and unbounded UPDATE/DELETE require human approval.
            Where the client supports elicitation the user is asked directly; otherwise
            the call is refused with a confirmation token to be re-supplied after you
            have relayed the reason to the user.
            """
            result = await query_write(
                conn_manager,
                config,
                audit,
                tokens,
                sql,
                confirm_token=confirm_token,
                timeout_ms=timeout_ms,
                ctx=ctx,
            )
            return result.to_dict()

    @mcp.tool(name="query.explain")
    @tool_boundary
    async def query_explain_tool(
        sql: str,
        analyze: bool = False,
        confirm_token: str | None = None,
        timeout_ms: int | None = None,
        summarize: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Explain a statement and return the plan plus actionable verdicts.

        analyze=false (default) plans without executing — always safe. analyze=true
        executes the statement to collect real timings; for a mutating statement that
        runs inside a transaction which is always rolled back, and requires the same
        confirmation token as query.write.

        summarize=true additionally asks your own model (via MCP sampling) for a prose
        walkthrough. It costs your tokens and is skipped silently if your client does
        not support sampling — the plan and the deterministic verdicts are returned
        either way.
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
            summarize=summarize,
            ctx=ctx,
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
                conn_manager,
                config,
                audit,
                tokens,
                plan_id,
                confirm_token=confirm_token,
                name=name,
            )

    @mcp.tool(name="migration.describe")
    @tool_boundary
    async def migration_describe_tool(
        description: str,
        allow_drops: bool = False,
        dry_run: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Plan a schema change described in plain English.

        Uses MCP sampling — *your* model does the English-to-target translation, so this
        server needs no API key of its own. The model only proposes a target schema; the
        SQL, the lock analysis and the confirmation gate are all still produced by the
        deterministic planner, and the interpretation is returned so you can check it.

        Requires a client that supports sampling; use migration.plan with an explicit
        target otherwise.
        """
        return await migration_describe(
            conn_manager,
            config,
            description,
            allow_drops=allow_drops,
            dry_run=dry_run,
            ctx=ctx,
        )

    @mcp.tool(name="migration.history")
    @tool_boundary
    async def migration_history_tool(limit: int = 20) -> dict[str, Any]:
        """Applied-migration history from the ledger, including any interrupted
        (in_flight) migration that needs manual resolution."""
        return await migration_history(conn_manager, limit=limit)

    @mcp.tool(name="env.topology")
    @tool_boundary
    async def env_topology_tool(all_containers: bool = False) -> dict[str, Any]:
        """Discover containers, compose projects, ports and health, and identify which
        container serves this server's DSN (matched by published host port).

        Container environment variables are never returned — they hold credentials.
        """
        return await env_topology(config, all_containers=all_containers)

    @mcp.tool(name="env.correlate")
    @tool_boundary
    async def env_correlate_tool() -> dict[str, Any]:
        """Join db.health findings with the database container's resource usage and
        return plain-language hints about whether container pressure explains the
        database's symptoms."""
        report = await db_health(conn_manager)
        findings = [f.to_dict() for f in report.findings]
        return await env_correlate(config, findings)

    @mcp.tool(name="container.logs")
    @tool_boundary
    async def container_logs_tool(
        name: str,
        tail: int = 100,
        min_severity: str | None = None,
        since_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Tail a container's logs, optionally filtered to a minimum Postgres severity
        (DEBUG/INFO/NOTICE/LOG/WARNING/ERROR/FATAL/PANIC)."""
        return await container_logs(
            name, tail=tail, min_severity=min_severity, since_seconds=since_seconds
        )

    @mcp.tool(name="container.stats")
    @tool_boundary
    async def container_stats_tool(name: str) -> dict[str, Any]:
        """CPU, memory and IO snapshot for a container. Takes about a second: a CPU
        percentage requires two samples to compute a delta."""
        return await container_stats(name)

    if config.approval_mode:

        @mcp.tool(name="container.restart")
        @tool_boundary
        async def container_restart_tool(
            name: str, confirm_token: str | None = None, timeout: int = 10
        ) -> dict[str, Any]:
            """Restart a container. Requires --approval-mode AND a confirmation token.
            Dropping connections is disruptive; relay the reason to the user first."""
            return await container_restart(
                config, audit, tokens, name, confirm_token=confirm_token, timeout=timeout
            )

        @mcp.tool(name="container.exec")
        @tool_boundary
        async def container_exec_tool(
            name: str, command: list[str], confirm_token: str | None = None
        ) -> dict[str, Any]:
            """Run a read-only diagnostic command inside a container. Requires
            --approval-mode AND a confirmation token, and the command must be in the
            diagnostic allowlist — this tool does not offer an arbitrary shell."""
            return await container_exec(
                config, audit, tokens, name, command, confirm_token=confirm_token
            )

    # --- resources: application-controlled, read-only context ---------------------
    # A client can attach these without the model spending a turn on a tool call.
    # Everything here mirrors data already reachable through tools, so resources add
    # no capability and no new attack surface.

    @mcp.resource("pgops://schema", mime_type="application/json")
    async def schema_res() -> str:
        """Full schema: tables, columns, constraints, indexes, extensions."""
        return await schema_resource(conn_manager)

    @mcp.resource("pgops://schema/summary", mime_type="application/json")
    async def schema_summary_res() -> str:
        """Table names, row estimates and sizes — the cheap version to attach."""
        return await schema_summary_resource(conn_manager)

    @mcp.resource("pgops://schema/{table}", mime_type="application/json")
    async def table_res(table: str) -> str:
        """One table's full definition."""
        return await table_resource(conn_manager, table)

    @mcp.resource("pgops://health", mime_type="application/json")
    async def health_res() -> str:
        """Current health snapshot with severities."""
        return await health_resource(conn_manager)

    @mcp.resource("pgops://migrations", mime_type="application/json")
    async def migrations_res() -> str:
        """Migration ledger history, including interrupted migrations."""
        return await migrations_resource(conn_manager)

    @mcp.resource("pgops://audit/recent", mime_type="application/json")
    async def audit_res() -> str:
        """Recent audit activity — metadata only; SQL text is deliberately omitted."""
        return audit_resource(config)

    @mcp.resource("pgops://config", mime_type="application/json")
    async def config_res() -> str:
        """This server's effective safety configuration (never the DSN)."""
        return config_resource(config)

    # --- prompts: user-invoked workflows -------------------------------------------
    # These encode the *order* to use the tools in and what to do with the answers,
    # which no individual tool can express.

    @mcp.prompt(name="diagnose-slow-query")
    def diagnose_slow_query_prompt(sql: str) -> str:
        """Investigate why a query is slow, using evidence rather than guesswork."""
        return diagnose_slow_query(sql)

    @mcp.prompt(name="plan-safe-migration")
    def plan_safe_migration_prompt(description: str) -> str:
        """Plan a schema change for zero downtime, with lock impact reviewed first."""
        return plan_safe_migration(description)

    @mcp.prompt(name="incident-triage")
    def incident_triage_prompt() -> str:
        """Triage a misbehaving database, cheapest and most-likely checks first."""
        return incident_triage()

    @mcp.prompt(name="review-index-health")
    def review_index_health_prompt() -> str:
        """Review indexes, respecting the statistics observation window."""
        return review_index_health()

    @mcp.prompt(name="explain-safety-model")
    def explain_safety_model_prompt() -> str:
        """Explain what this server will and will not permit, and why."""
        return explain_safety_model()

    # Autocomplete for the {table} argument of pgops://schema/{table}, so a client can
    # offer a picker instead of the user having to know the name first.
    register_completions(mcp, conn_manager)

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
    parser.add_argument(
        "--read-only", action="store_true", default=None, help="disable write tools"
    )
    parser.add_argument(
        "--audit-log",
        default=None,
        help="path to the JSONL audit log (default ~/.pgops/audit.jsonl)",
    )
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="connect, introspect, print a summary, then exit (no MCP transport)",
    )
    parser.add_argument("--verbose", action="store_true", help="debug-level logging on stderr")
    parser.add_argument(
        "--approval-mode",
        action="store_true",
        default=None,
        help="permit container mutations (restart/exec); off by default because Docker "
        "socket access is equivalent to root on the host",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="stdio (default, local, no auth needed) or http (remote, requires --public-key)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8000, help="HTTP bind port")
    parser.add_argument(
        "--public-key", default=None, help="PEM public key used to verify agent tokens (HTTP)"
    )

    # Key management lives in the same binary so the whole workflow is one install.
    sub = parser.add_subparsers(dest="command")

    keygen = sub.add_parser("keygen", help="generate the RSA keypair for agent tokens")
    keygen.add_argument("--key-dir", default="~/.pgops/keys", help="where to write the keypair")

    token = sub.add_parser("issue-token", help="mint a bearer token for an agent")
    token.add_argument("--subject", required=True, help="agent identity, recorded in the audit log")
    token.add_argument(
        "--key", default="~/.pgops/keys/pgops_private.pem", help="private key to sign with"
    )
    token.add_argument(
        "--scope",
        action="append",
        choices=[s.value for s in Scope],
        help="repeatable; defaults to pgops:read only",
    )
    token.add_argument("--expires-in", type=int, default=30, help="lifetime in days")

    sub.add_parser("scopes", help="show which scope each tool requires")

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


def _run_keygen(args: argparse.Namespace) -> None:
    """Generate the RSA keypair used to sign and verify agent tokens."""
    directory = Path(args.key_dir).expanduser()
    material = generate_keypair()
    private_path, public_path = material.save(directory)
    print(f"private key (keep secret, used only to issue tokens): {private_path}")
    print(f"public key  (give to the server to verify tokens):    {public_path}")
    print()
    print("Issue a token for an agent:")
    print(f"  pgops-mcp issue-token --subject my-agent --key {private_path}")
    print()
    print("Run the server with HTTP transport:")
    print(f"  pgops-mcp --transport http --public-key {public_path}")


def _run_issue_token(args: argparse.Namespace) -> None:
    key_path = Path(args.key).expanduser()
    try:
        private_key = key_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"pgops-mcp: cannot read private key {key_path}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    try:
        token = issue_token(
            private_key,
            subject=args.subject,
            scopes=args.scope or None,
            expires_in_seconds=args.expires_in * 86400,
        )
    except PgopsError as exc:
        print(f"pgops-mcp: {exc.message}", file=sys.stderr)
        raise SystemExit(1) from exc

    scopes = args.scope or [Scope.READ.value]
    print(token)
    print(file=sys.stderr)
    print(f"subject: {args.subject}", file=sys.stderr)
    print(f"scopes:  {scopes}", file=sys.stderr)
    print(f"expires: {args.expires_in} days", file=sys.stderr)
    if Scope.READ.value in scopes and len(scopes) == 1:
        print("this token cannot write or modify containers", file=sys.stderr)


def main() -> None:
    args = parse_args()

    if args.command == "keygen":
        _run_keygen(args)
        return
    if args.command == "issue-token":
        _run_issue_token(args)
        return
    if args.command == "scopes":
        print(describe_scopes())
        return

    configure_logging(logging.DEBUG if args.verbose else logging.INFO)
    try:
        config = PgopsConfig.from_env(
            dsn=args.dsn,
            read_only=args.read_only,
            audit_path=Path(args.audit_log) if args.audit_log else None,
            approval_mode=args.approval_mode,
        )
    except PgopsError as exc:
        print(f"pgops-mcp: {exc.to_dict()}", file=sys.stderr)
        raise SystemExit(1) from exc

    if args.selfcheck:
        asyncio.run(_selfcheck(config))
        return

    # Auth is bound to the transport, not offered as a global flag. Over stdio there is
    # no remote caller to authenticate and requiring a token would be theatre; over HTTP
    # the port is reachable and running without auth would expose a database operator to
    # anyone who can route to it. So HTTP refuses to start without a key.
    auth = None
    if args.transport == "http":
        if not args.public_key:
            print(
                "pgops-mcp: --transport http requires --public-key.\n"
                "  generate one with:  pgops-mcp keygen\n"
                "  refusing to expose database tools on a network port without auth.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        auth = build_verifier(load_public_key(Path(args.public_key).expanduser()))

    conn_manager = ConnectionManager(config)

    async def _run() -> None:
        await conn_manager.start()
        try:
            mcp = build_server(config, conn_manager, auth=auth)
            if args.transport == "http":
                logger = logging.getLogger("pgops")
                logger.info("serving MCP over HTTP on %s:%s", args.host, args.port)
                await mcp.run_async(transport="http", host=args.host, port=args.port)
            else:
                await mcp.run_async(transport="stdio")
        finally:
            await conn_manager.stop()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
