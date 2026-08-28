"""Drive the server as a real MCP subprocess over stdio.

Every other test calls the tool functions or `server.call_tool()` in-process. That
proves the logic works; it does not prove the *server* works. This test launches the
installed console script the way Claude Desktop / Cursor launch it and speaks MCP
JSON-RPC to it, which is the only way to catch:

- a broken console-script entry point or packaging change,
- anything printed to **stdout**, which corrupts the protocol stream (the failure this
  guards is silent and looks like a client bug),
- startup ordering problems — pools opened after the transport is already serving,
- tool schemas that don't survive being serialized to a real client.

Marked `slow` because it pays process startup; run with `-m "not slow"` to skip.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from pgops.connections import ConnectionManager

pytestmark = pytest.mark.slow


def _transport(dsn: str, audit_path: Path) -> StdioTransport:
    env = dict(os.environ)
    env["PGOPS_DSN"] = dsn
    env["PGOPS_AUDIT_LOG"] = str(audit_path)
    # `-m pgops` rather than the console script: exercises the same entry point without
    # depending on the venv's bin directory being on PATH inside the test runner.
    return StdioTransport(command=sys.executable, args=["-m", "pgops"], env=env)


async def test_server_starts_and_advertises_tools(dsn: str, tmp_path: Path) -> None:
    async with Client(_transport(dsn, tmp_path / "audit.jsonl")) as client:
        names = {t.name for t in await client.list_tools()}
    assert {
        "schema.inspect",
        "query.read",
        "query.write",
        "query.explain",
        "index.advise",
        "db.health",
    } <= names


async def test_tools_answer_over_the_wire(
    dsn: str, tmp_path: Path, conn_manager: ConnectionManager
) -> None:
    # conn_manager is requested for its side effect: it seeds the `items` table, so
    # there is something for schema.inspect to find. Without it this test passes or
    # fails depending on which other tests ran first.
    async with Client(_transport(dsn, tmp_path / "audit.jsonl")) as client:
        schema = await client.call_tool("schema.inspect", {"level": "full"})
        assert any(t["name"] == "items" for t in schema.data["tables"])

        read = await client.call_tool("query.read", {"sql": "SELECT 1 AS n"})
        assert read.data["rows"] == [{"n": 1}]

        health = await client.call_tool("db.health", {})
        assert health.data["findings"]


async def test_safety_guarantees_hold_over_the_wire(dsn: str, tmp_path: Path) -> None:
    """The confirm flow, end to end, through a real transport."""
    audit_path = tmp_path / "audit.jsonl"
    async with Client(_transport(dsn, audit_path)) as client:
        await client.call_tool(
            "query.write",
            {"sql": "CREATE TABLE IF NOT EXISTS stdio_items (id serial PRIMARY KEY)"},
        )
        await client.call_tool(
            "query.write",
            {"sql": "INSERT INTO stdio_items SELECT FROM generate_series(1, 5)"},
        )

        refusal = await client.call_tool("query.write", {"sql": "DELETE FROM stdio_items"})
        error = refusal.data["error"]
        assert error["code"] == "CONFIRMATION_REQUIRED"

        # nothing deleted
        count = await client.call_tool(
            "query.read", {"sql": "SELECT count(*) AS n FROM stdio_items"}
        )
        assert count.data["rows"][0]["n"] == 5

        # a token for this statement must not execute a different one
        token = error["hint"].split("confirm_token=")[1].split("'")[1]
        mismatch = await client.call_tool(
            "query.write", {"sql": "DROP TABLE stdio_items", "confirm_token": token}
        )
        assert mismatch.data["error"]["code"] == "CONFIRMATION_MISMATCH"

        # the real statement, with its own token, does execute
        confirmed = await client.call_tool(
            "query.write", {"sql": "DELETE FROM stdio_items", "confirm_token": token}
        )
        assert confirmed.data["rows_affected"] == 5

        await client.call_tool(
            "query.write",
            {
                "sql": "DROP TABLE stdio_items",
                "confirm_token": (
                    (await client.call_tool("query.write", {"sql": "DROP TABLE stdio_items"}))
                    .data["error"]["hint"]
                    .split("confirm_token=")[1]
                    .split("'")[1]
                ),
            },
        )

    # the subprocess wrote its own audit trail
    entries = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
    verdicts = [e["verdict"] for e in entries]
    assert "refused_pending_confirmation" in verdicts
    assert "refused_bad_token" in verdicts
    assert "executed" in verdicts


async def test_stdout_carries_only_protocol_traffic(dsn: str, tmp_path: Path) -> None:
    """Nothing but JSON-RPC may reach stdout.

    FastMCP prints a startup banner and pgops logs to stderr; if either ever moved to
    stdout the client would see malformed protocol frames. The client completing a
    handshake and a call is the assertion — a polluted stream fails to parse — and this
    test states that intent explicitly so the requirement isn't lost.
    """
    import asyncio

    env = dict(os.environ)
    env["PGOPS_DSN"] = dsn
    env["PGOPS_AUDIT_LOG"] = str(tmp_path / "audit.jsonl")

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pgops",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        # Send nothing; give the server a moment to start up and emit its banner.
        await asyncio.sleep(3)
        assert proc.stdout is not None
        try:
            early_stdout = await asyncio.wait_for(proc.stdout.read(4096), timeout=1)
        except TimeoutError:
            early_stdout = b""
    finally:
        proc.terminate()
        await proc.wait()

    # Startup chatter must not have landed on stdout before any request was made.
    assert early_stdout == b"", early_stdout[:400]


async def test_read_only_mode_hides_write_tool_over_the_wire(dsn: str, tmp_path: Path) -> None:
    env = dict(os.environ)
    env["PGOPS_DSN"] = dsn
    env["PGOPS_READ_ONLY"] = "true"
    env["PGOPS_AUDIT_LOG"] = str(tmp_path / "audit.jsonl")
    transport = StdioTransport(command=sys.executable, args=["-m", "pgops"], env=env)

    async with Client(transport) as client:
        names = {t.name for t in await client.list_tools()}
    assert "query.write" not in names
    assert "query.read" in names


def test_startup_writes_nothing_to_stdout_and_no_vendor_banner(tmp_path: Path, dsn: str) -> None:
    """Under stdio, stdout is the JSON-RPC channel and stderr is the server's log.

    Two separate properties. stdout must be empty or the transport is corrupt. stderr is
    what a client surfaces to an operator as *this server's* log, and FastMCP's default
    startup banner puts ASCII art and a link to an unrelated hosting product there.
    """
    env = {
        **os.environ,
        "PGOPS_DSN": dsn,
        "PGOPS_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "pgops"],
        input=b"",
        capture_output=True,
        env=env,
        timeout=60,
        # The server exits non-zero when stdin closes immediately; the streams are what
        # this asserts on, not the exit code.
        check=False,
    )
    assert proc.stdout == b"", f"stdio transport polluted stdout: {proc.stdout[:200]!r}"
    stderr = proc.stderr.decode("utf-8", "replace").lower()
    assert "prefect" not in stderr and "horizon" not in stderr, (
        "a third-party product banner is being written to this server's log"
    )
