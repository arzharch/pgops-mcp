"""End-to-end tests through the real FastMCP server object.

These exist because unit-testing the tool functions directly missed two production
bugs: `schema.inspect(level="full")` returned raw `bytes` from a catalog column and
`db.health` returned a `Decimal`, both of which are perfectly fine Python objects and
both of which fail when the MCP layer encodes the result as JSON. Calling through
`server.call_tool` exercises the serialization boundary an agent actually hits.
"""

from __future__ import annotations

import json

from fastmcp import FastMCP

from pgops.__main__ import build_server
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager

DOCUMENTED_TOOL_NAMES = {"schema.inspect", "query.read", "db.health"}


async def _server(conn_manager: ConnectionManager, config: PgopsConfig) -> FastMCP:
    return build_server(config, conn_manager)


async def test_registered_tool_names_match_docs(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    """Tool names are a public contract (docs/TOOLS.md, and whatever an agent was told
    to call). They must not drift with the Python function names behind them."""
    server = await _server(conn_manager, config)
    names = {t.name for t in await server.list_tools()}
    assert DOCUMENTED_TOOL_NAMES <= names, names


async def test_every_tool_result_is_json_encodable(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    server = await _server(conn_manager, config)
    calls = [
        ("schema.inspect", {"level": "full"}),
        ("schema.inspect", {"level": "tables"}),
        ("schema.inspect", {"level": "summary"}),
        ("query.read", {"sql": "SELECT * FROM items ORDER BY id", "limit": 5}),
        ("db.health", {}),
    ]
    for name, args in calls:
        result = await server.call_tool(name, args)
        assert result.is_error is False, (name, result)
        json.dumps(result.structured_content)


async def test_refusal_returns_structured_error_not_exception(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    server = await _server(conn_manager, config)
    result = await server.call_tool("query.read", {"sql": "DROP TABLE items"})
    # a refusal is a normal, parseable result — not a transport-level error the agent
    # has to interpret from a stack trace
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["error"]["code"] == "CLASSIFICATION_REFUSED"


async def test_postgres_error_surfaces_as_structured_error(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    server = await _server(conn_manager, config)
    result = await server.call_tool("query.read", {"sql": "SELECT * FROM no_such_table"})
    assert result.structured_content is not None
    assert result.structured_content["error"]["code"] == "INVALID_ARGUMENT"
